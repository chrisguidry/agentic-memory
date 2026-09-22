// Package bastion serves the socket: one long-lived process per person that
// holds the service's address and authorization, keeps one connection to the
// service warm, and answers or proxies everything a client asks.
package bastion

import (
	"os"
	"path/filepath"
	"strconv"
	"time"

	"github.com/chrisguidry/agentic-memory/host/socket"
)

// Config is everything the bastion reads from its environment. The systemd
// unit reads these from `~/.config/agentic-memory/environment`, and nothing
// else on the machine holds them.
type Config struct {
	Socket        string
	Service       string
	Authorization string
	Deadline      time.Duration
	Limit         int
	Machine       string
	StateDir      string
}

// Configure reads the environment. The defaults are the ones the plan names,
// so a bastion started with an address and an authorization and nothing else
// works.
func Configure() Config {
	return Config{
		Socket:        socket.Path(),
		Service:       os.Getenv("AGENTIC_MEMORY_SERVICE"),
		Authorization: os.Getenv("AGENTIC_MEMORY_AUTHORIZATION"),
		Deadline:      milliseconds("AGENTIC_MEMORY_RECALL_DEADLINE_MS", 500*time.Millisecond),
		Limit:         number("AGENTIC_MEMORY_RECALL_LIMIT", 10),
		Machine:       machine(),
		StateDir:      stateDir(),
	}
}

func machine() string {
	if named := os.Getenv("AGENTIC_MEMORY_MACHINE"); named != "" {
		return named
	}
	name, err := os.Hostname()
	if err != nil {
		return "unknown"
	}
	return name
}

// stateDir is where the offsets are kept. XDG puts state that should survive a
// restart but that nobody would miss if it were lost under `~/.local/state`.
// Losing an offset means re-sending a file the service already holds, which is
// exactly that kind of loss.
func stateDir() string {
	home := os.Getenv("XDG_STATE_HOME")
	if home == "" {
		found, err := os.UserHomeDir()
		if err != nil {
			return filepath.Join(".", "agentic-memory")
		}
		home = filepath.Join(found, ".local", "state")
	}
	return filepath.Join(home, "agentic-memory")
}

func number(name string, fallback int) int {
	found, err := strconv.Atoi(os.Getenv(name))
	if err != nil || found <= 0 {
		return fallback
	}
	return found
}

func milliseconds(name string, fallback time.Duration) time.Duration {
	found, err := strconv.Atoi(os.Getenv(name))
	if err != nil || found <= 0 {
		return fallback
	}
	return time.Duration(found) * time.Millisecond
}
