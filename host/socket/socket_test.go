package socket_test

import (
	"net"
	"os"
	"path/filepath"
	"testing"

	"github.com/chrisguidry/agentic-memory/host/socket"
)

func TestTheSocketIsCreatedForThisUserOnly(t *testing.T) {
	path := filepath.Join(t.TempDir(), "runtime", socket.Name)
	listener, err := socket.Listen(path)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()

	found, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if mode := found.Mode().Perm(); mode != 0o600 {
		t.Errorf("got mode %o, want 600", mode)
	}
}

func TestAStaleSocketIsReplaced(t *testing.T) {
	path := filepath.Join(t.TempDir(), socket.Name)
	stale, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	// A crash leaves the file behind with nothing listening on it.
	stale.Close()
	if err := os.WriteFile(path, nil, 0o600); err != nil {
		t.Skipf("this filesystem does not keep the stale socket: %v", err)
	}

	listener, err := socket.Listen(path)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()

	connection, err := net.Dial("unix", path)
	if err != nil {
		t.Fatalf("the new socket refused a connection: %v", err)
	}
	connection.Close()
}

func TestThePathComesFromTheEnvironment(t *testing.T) {
	t.Setenv("AGENTIC_MEMORY_SOCKET", "/somewhere/else.sock")
	if found := socket.Path(); found != "/somewhere/else.sock" {
		t.Errorf("got %q, want %q", found, "/somewhere/else.sock")
	}
	t.Setenv("AGENTIC_MEMORY_SOCKET", "")
	t.Setenv("XDG_RUNTIME_DIR", "/run/user/test")
	if found := socket.Path(); found != "/run/user/test/"+socket.Name {
		t.Errorf("got %q", found)
	}
}
