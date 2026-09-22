package claudecode

import (
	"bufio"
	"fmt"
	"io"
	"net"
	"net/http"
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
// It writes nothing on any failure and exits zero whatever happened, because
// Claude Code adds a `UserPromptSubmit` hook's stdout to the conversation and
// shows a hook's stderr to the person. It touches the network and the
// filesystem nowhere beyond the socket.
func Hook(stdin io.Reader, stdout io.Writer) {
	deadline := time.Now().Add(Deadline)
	body, err := io.ReadAll(stdin)
	if err != nil {
		return
	}
	connection, err := net.DialTimeout("unix", socket.Path(), time.Until(deadline))
	if err != nil {
		return
	}
	defer connection.Close()
	if err := connection.SetDeadline(deadline); err != nil {
		return
	}

	// The request is written by hand rather than through http.Client, so the
	// hook costs one connect, one write, and one read.
	request := fmt.Sprintf("POST /claude-code/hooks HTTP/1.1\r\nHost: bastion\r\n"+
		"Content-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n", len(body))
	if _, err := connection.Write(append([]byte(request), body...)); err != nil {
		return
	}
	response, err := http.ReadResponse(bufio.NewReader(connection), nil)
	if err != nil {
		return
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return
	}
	io.Copy(stdout, response.Body)
}
