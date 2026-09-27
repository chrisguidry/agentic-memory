package claudecode

import (
	"bufio"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"time"

	"github.com/chrisguidry/agentic-memory/host/socket"
)

// Deadline bounds the whole hook. Claude Code blocks the turn until the hook
// returns, so a wedged bastion cannot hold the turn to Claude Code's own
// timeout.
const Deadline = 2 * time.Second

// Hook sends the payload Claude Code gives on stdin to the bastion and writes
// the answer to stdout.
//
// It writes nothing on any failure of the socket or the bastion and exits
// zero, because Claude Code adds a `UserPromptSubmit` hook's stdout to the
// conversation and shows a hook's stderr to the person. It touches the network
// and the filesystem nowhere beyond the socket.
//
// The one exception is an AGENTIC_MEMORY_PROBE value other than 1, true, 0,
// false, or empty. Then it forwards nothing, writes why to stderr, and exits
// 1. Claude Code shows that as a hook error and goes on without memory.
func Hook(stdin io.Reader, stdout, stderr io.Writer) int {
	deadline := time.Now().Add(Deadline)
	route, err := routeFor(os.Getenv("AGENTIC_MEMORY_PROBE"))
	if err != nil {
		fmt.Fprintln(stderr, "agentic-memory:", err)
		return 1
	}
	body, err := io.ReadAll(stdin)
	if err != nil {
		return 0
	}
	connection, err := net.DialTimeout("unix", socket.Path(), time.Until(deadline))
	if err != nil {
		return 0
	}
	defer connection.Close()
	if err := connection.SetDeadline(deadline); err != nil {
		return 0
	}

	// The request is written by hand rather than through http.Client, so the
	// hook costs one connect, one write, and one read.
	request := fmt.Sprintf("POST %s HTTP/1.1\r\nHost: bastion\r\n"+
		"Content-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n", route, len(body))
	if _, err := connection.Write(append([]byte(request), body...)); err != nil {
		return 0
	}
	response, err := http.ReadResponse(bufio.NewReader(connection), nil)
	if err != nil {
		return 0
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return 0
	}
	io.Copy(stdout, response.Body)
	return 0
}
