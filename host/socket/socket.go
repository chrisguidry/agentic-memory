// Package socket is where the bastion listens and every client connects. Both
// sides read the same address from the same environment, so neither has to
// hold the other's configuration.
package socket

import (
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strconv"
)

// Name is the socket's file name under the runtime directory.
const Name = "agentic-memory.sock"

// Path is where the socket is. `AGENTIC_MEMORY_SOCKET` overrides it for every
// client and for the bastion.
func Path() string {
	if named := os.Getenv("AGENTIC_MEMORY_SOCKET"); named != "" {
		return named
	}
	if runtime := os.Getenv("XDG_RUNTIME_DIR"); runtime != "" {
		return filepath.Join(runtime, Name)
	}
	return filepath.Join("/run/user", strconv.Itoa(os.Getuid()), Name)
}

// Listen returns the socket the bastion serves on.
//
// systemd creates the socket itself and hands it over as file descriptor 3,
// which is how the first client's connection starts the bastion. Run by hand,
// the bastion creates the socket at path.
func Listen(path string) (net.Listener, error) {
	if inherited, err := fromSystemd(); inherited != nil || err != nil {
		return inherited, err
	}

	// A crash leaves the socket file behind, and a socket nothing listens on
	// refuses every connection, so the stale one goes before the new one.
	if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	listener, err := net.Listen("unix", path)
	if err != nil {
		return nil, err
	}
	// The kernel is what keeps other users out, so the mode is the whole
	// access control on the socket.
	if err := os.Chmod(path, 0o600); err != nil {
		listener.Close()
		return nil, err
	}
	return listener, nil
}

// fromSystemd returns the listener systemd passed, or nothing when systemd
// started nothing. `LISTEN_PID` is checked because the variables are inherited
// by every child, and a child's descriptor 3 is its own.
func fromSystemd() (net.Listener, error) {
	count, err := strconv.Atoi(os.Getenv("LISTEN_FDS"))
	if err != nil || count < 1 {
		return nil, nil
	}
	if pid, err := strconv.Atoi(os.Getenv("LISTEN_PID")); err != nil || pid != os.Getpid() {
		return nil, nil
	}
	file := os.NewFile(3, Name)
	if file == nil {
		return nil, fmt.Errorf("systemd named %d sockets and passed none", count)
	}
	defer file.Close()
	return net.FileListener(file)
}
