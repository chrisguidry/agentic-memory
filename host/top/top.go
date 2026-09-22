// Package top draws what the loop is producing.
//
// Three panels, because the pipeline has three places to look. The top one is
// what is worth remembering, ranked, which is the list a turn would read. The
// middle one is what the writer has just produced, in the order it produced it,
// so a statement that ranks low is still seen arriving. The bottom one is what
// the classifier is reading, which includes the messages it read and wrote
// nothing for.
//
// The last two are what make a message that never became a memory visible. A
// message below every threshold produces no row, so only the readings panel can
// show it.
//
// It polls the bastion's socket rather than reading the database, so it shows
// what a turn would actually get. It reads the scope of the directory you run
// it from, the same way a session derives its own, so the default view is what
// that directory would be given.
//
// Three panels of ten want fifty-five rows of terminal, so each count is an
// upper bound and the frame gives way from the top down until it fits. The
// status line says how many were hidden.
package top

import (
	"context"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"
	"time"
	"unsafe"

	"github.com/chrisguidry/agentic-memory/host/scope"
	"github.com/chrisguidry/agentic-memory/host/socket"
)

// options is what the command line asked for.
type options struct {
	scope   string
	limit   int
	written int
	read    int
	every   float64
	socket  string
}

// size is the terminal the frame is drawn into.
type size struct {
	rows    int
	columns int
}

// fallbackRows and fallbackColumns are the terminal a program that is not
// attached to one gets.
const (
	fallbackRows    = 24
	fallbackColumns = 80
)

// Run draws the three panels until the person stops it, and returns the exit
// status.
func Run(args []string, out io.Writer) int {
	flags := flag.NewFlagSet("top", flag.ContinueOnError)
	flags.Usage = func() {
		fmt.Fprint(flags.Output(), usage)
		flags.PrintDefaults()
	}
	var asked options
	var everyScope bool
	flags.StringVar(&asked.scope, "scope", "", "a scope to read from, rather than this directory's")
	flags.BoolVar(&everyScope, "all-scopes", false, "read from every scope, rather than this directory's")
	flags.IntVar(&asked.limit, "limit", 10, "how many top-of-mind statements")
	flags.IntVar(&asked.written, "new", 10, "how many recently written statements")
	flags.IntVar(&asked.read, "read", 10, "how many recent readings")
	flags.Float64Var(&asked.every, "every", 2.0, "seconds between polls")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	asked.socket = socket.Path()

	switch {
	case everyScope:
		asked.scope = ""
	case asked.scope == "":
		home, _ := os.UserHomeDir()
		cwd, err := os.Getwd()
		if err != nil {
			fmt.Fprintf(os.Stderr, "cannot read this directory: %v\n", err)
			return 1
		}
		named := ""
		asked.scope, named = scope.Of(cwd, home)
		fmt.Fprintf(out, "scope %s, named by its %s\n", asked.scope, named)
	}

	return watch(asked, Dial(asked.socket), out)
}

const usage = `agentic-memory top watches what the memory loop is producing.

Three panels: what is worth remembering for this directory's scope, what the
writer has just produced, and what the classifier has just read. Each count is
an upper bound, and the frame gives way from the top down until it fits the
terminal.

`

// watch polls and redraws until the person interrupts it.
func watch(asked options, client *Client, out io.Writer) int {
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(stop)

	// The caret is hidden for the whole run and shown again on the way out, so
	// an interrupted watch does not leave the terminal without one.
	fmt.Fprint(out, hideCaret)
	defer fmt.Fprint(out, showCaret+"\n")

	every := time.Duration(asked.every * float64(time.Second))
	if every < time.Millisecond {
		every = time.Millisecond
	}
	ticker := time.NewTicker(every)
	defer ticker.Stop()

	var now state
	for {
		poll(asked, client, &now)
		fmt.Fprint(out, homeClear+frame(asked, now, terminal(out), time.Now().UTC()))
		select {
		case <-stop:
			return 0
		case <-ticker.C:
		}
	}
}

// poll gets the three lists, and remembers how they failed rather than ending
// the watch. The lists from the last poll that worked stay where they are, so a
// bastion that comes back redraws what it has.
func poll(asked options, client *Client, now *state) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	memories, err := client.Memories(ctx, asked.scope, asked.limit, "rank")
	if err != nil {
		now.problem = unreachable(asked.socket, err)
		return
	}
	written, err := client.Memories(ctx, asked.scope, asked.written, "newest")
	if err != nil {
		now.problem = unreachable(asked.socket, err)
		return
	}
	readings, err := client.Classifications(ctx, asked.read)
	if err != nil {
		now.problem = unreachable(asked.socket, err)
		return
	}
	now.memories, now.written, now.readings, now.problem = memories, written, readings, ""
}

func unreachable(path string, err error) string {
	return fmt.Sprintf("cannot reach the bastion at %s: %v", path, err)
}

// terminal returns the size of the terminal a writer is attached to. A writer
// that is not a terminal, such as a file, gets the fallback.
func terminal(out io.Writer) size {
	file, ok := out.(*os.File)
	if !ok {
		return size{rows: fallbackRows, columns: fallbackColumns}
	}
	var window struct{ rows, columns, width, height uint16 }
	_, _, errno := syscall.Syscall(
		syscall.SYS_IOCTL,
		file.Fd(),
		syscall.TIOCGWINSZ,
		uintptr(unsafe.Pointer(&window)),
	)
	if errno != 0 || window.rows == 0 || window.columns == 0 {
		return size{rows: fallbackRows, columns: fallbackColumns}
	}
	return size{rows: int(window.rows), columns: int(window.columns)}
}
