"""Provide an isolated SSH target for the real CLI command-approval smoke test."""

from contextlib import contextmanager
from pathlib import Path
import shlex
import socket
import subprocess
import threading

import paramiko


@contextmanager
def ssh_probe(root: Path, probes: list):
    """Yield a one-shot OpenSSH command and count authenticated remote executions."""
    directory = root / ".ssh"
    directory.mkdir(mode=0o700)
    identity = directory / "fixture_ed25519"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(identity)],
        check=True, capture_output=True, timeout=10,
    )
    public_key = paramiko.Ed25519Key.from_private_key_file(str(identity))
    host_key = paramiko.RSAKey.generate(2048)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    known_hosts = directory / "known_hosts"
    known_hosts.write_text(f"[127.0.0.1]:{port} {host_key.get_name()} {host_key.get_base64()}\n")
    configuration = directory / "config"
    configuration.write_text(
        "Host ssh-fixture\n"
        "    HostName 127.0.0.1\n"
        f"    Port {port}\n"
        "    User fixture\n"
        f"    IdentityFile {identity}\n"
        f"    UserKnownHostsFile {known_hosts}\n"
        "    IdentitiesOnly yes\n"
        "    IdentityAgent none\n"
        "    StrictHostKeyChecking yes\n"
        "    BatchMode yes\n"
        "    ControlMaster no\n"
        "    ControlPath none\n"
        "    ControlPersist no\n"
        "    ClearAllForwardings yes\n"
        "    ForwardAgent no\n"
        "    RequestTTY no\n"
        "    ConnectTimeout 2\n"
        "    ConnectionAttempts 1\n"
    )
    configuration.chmod(0o600)
    known_hosts.chmod(0o600)
    command = shlex.join(["ssh", "-F", str(configuration), "-n", "ssh-fixture", "probe"])
    stopped = threading.Event()
    transports = []
    clients = []

    class Server(paramiko.ServerInterface):
        """Accept only the fixture identity and one fixed remote operation."""

        def __init__(self):
            """Track when the client has requested its remote command."""
            self.requested = threading.Event()
            self.command = None

        def get_allowed_auths(self, username):
            """Offer public-key authentication without password fallback."""
            return "publickey"

        def check_auth_publickey(self, username, key):
            """Authenticate only the generated test key and fixture account."""
            if username == "fixture" and key.asbytes() == public_key.asbytes():
                return paramiko.AUTH_SUCCESSFUL
            return paramiko.AUTH_FAILED

        def check_channel_request(self, kind, channel_id):
            """Allow a command session while rejecting forwarding channels."""
            return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

        def check_channel_exec_request(self, channel, command):
            """Record the requested operation for the target-side dispatcher."""
            self.command = command
            self.requested.set()
            return True

    def serve_client(connection):
        """Return a marker only after authenticated execution of the fixed command."""
        transport = paramiko.Transport(connection)
        transports.append(transport)
        try:
            transport.add_server_key(host_key)
            server = Server()
            transport.start_server(server=server)
            channel = transport.accept(5)
            if channel is None or not server.requested.wait(5):
                return
            if server.command == b"probe":
                probes.append("ssh-probe")
                channel.sendall(b"network-action-executed\n")
                channel.send_exit_status(0)
            else:
                channel.send_stderr(b"Operation denied\n")
                channel.send_exit_status(64)
            channel.close()
            stopped.wait(0.1)
        except (EOFError, OSError, paramiko.SSHException):
            # Unapproved sandboxed commands may close before authentication.
            pass
        finally:
            transport.close()

    def accept_connections():
        """Serve sequential CLI commands without keeping a reusable SSH session."""
        while not stopped.is_set():
            try:
                connection, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            client = threading.Thread(target=serve_client, args=(connection,), daemon=True)
            clients.append(client)
            client.start()

    acceptor = threading.Thread(target=accept_connections, daemon=True)
    acceptor.start()
    try:
        settings = subprocess.check_output(["ssh", "-G", "-F", str(configuration), "ssh-fixture"], text=True)
        effective = dict(line.split(" ", 1) for line in settings.splitlines())
        assert effective["identityfile"] == str(identity), settings
        assert effective["userknownhostsfile"] == str(known_hosts), settings
        assert effective["controlmaster"] in {"false", "no"}, settings
        assert effective.get("controlpath", "none") == "none", settings
        control = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=10)
        assert control.returncode == 0 and "network-action-executed" in control.stdout, (control.stdout, control.stderr)
        assert probes == ["ssh-probe"], probes
        probes.clear()
        yield command
    finally:
        stopped.set()
        listener.close()
        acceptor.join(2)
        for transport in transports:
            transport.close()
        for client in clients:
            client.join(2)
