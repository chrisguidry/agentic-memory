// Package label walks a sample of prompt-and-statement pairs and takes a
// person's judgment of each one: good, noise, or wrong.
//
// It draws from the same terminal pieces as `top`, because both commands draw
// one frame from whatever the bastion last answered and redraw it after every
// key or every poll. Where `top` watches three lists on its own clock, `label`
// waits on the person: nothing changes until a key says it should.
package label

import (
	"context"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"time"

	"github.com/chrisguidry/agentic-memory/host/socket"
	"github.com/chrisguidry/agentic-memory/host/terminal"
)

// options is what the command line asked for.
type options struct {
	sample string
	socket string
}

// Run walks the sample until the person quits, and returns the exit status.
func Run(args []string, out io.Writer) int {
	flags := flag.NewFlagSet("label", flag.ContinueOnError)
	flags.Usage = func() {
		fmt.Fprint(flags.Output(), usage)
		flags.PrintDefaults()
	}
	var asked options
	flags.StringVar(&asked.sample, "sample", "", "the sample to label")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	if asked.sample == "" {
		fmt.Fprintln(flags.Output(), "agentic-memory label needs -sample")
		return 2
	}
	asked.socket = socket.Path()

	restore := rawMode(os.Stdin)
	defer restore()
	// The caret is hidden for the whole run and shown again on the way out, so
	// a quit mid-frame does not leave the terminal without one.
	fmt.Fprint(out, terminal.HideCaret)
	defer fmt.Fprint(out, terminal.ShowCaret+"\n")

	return loop(asked, Dial(asked.socket), os.Stdin, out)
}

const usage = `agentic-memory label walks a sample of prompts and the statements they were
handed, and takes a person's judgment of each pair.

  g good   n noise   w wrong   s skip   q quit

`

// rawMode reads one key at a time with no local echo, and returns a function
// that restores the terminal. A line-buffered read would make every key wait
// on Enter, and echo would print the person's own keystrokes over the frame
// being drawn.
//
// stty is run rather than a terminal library, for the same reason the drawing
// in `terminal` is plain escape codes: the binary is shared with the Claude
// Code hook, and a library would add package initialization to every turn.
func rawMode(in *os.File) func() {
	set := func(args ...string) {
		cmd := exec.Command("stty", args...)
		cmd.Stdin = in
		_ = cmd.Run()
	}
	set("cbreak", "-echo")
	return func() { set("-cbreak", "echo") }
}

// readKey reads the one byte a keypress sends.
func readKey(in io.Reader) (byte, error) {
	buf := make([]byte, 1)
	if _, err := in.Read(buf); err != nil {
		return 0, err
	}
	return buf[0], nil
}

// judgment is what one keypress asks the loop to do.
type judgment struct {
	// label is "good", "noise", or "wrong" when the key judged the pair.
	label string
	skip  bool
	quit  bool
}

// interpret reads one key the person pressed. The second return is false for
// a key that means nothing, which the loop reads past without acting on it.
func interpret(key byte) (judgment, bool) {
	switch key {
	case 'g':
		return judgment{label: "good"}, true
	case 'n':
		return judgment{label: "noise"}, true
	case 'w':
		return judgment{label: "wrong"}, true
	case 's':
		return judgment{skip: true}, true
	case 'q', 3: // q, or the byte a terminal sends for Ctrl-C
		return judgment{quit: true}, true
	default:
		return judgment{}, false
	}
}

// api is what the loop needs from the service, narrowed so a test can stand
// in for it without a socket.
type api interface {
	Next(ctx context.Context, sample string, after int) (Next, error)
	Judge(ctx context.Context, pair Pair, label string) error
}

// loop draws a pair, waits for one meaningful key, and acts on it, until the
// person quits or the socket cannot be reached at all.
func loop(asked options, client api, in io.Reader, out io.Writer) int {
	ctx := context.Background()
	after := 0
	for {
		found, err := client.Next(ctx, asked.sample, after)
		problem := ""
		if err != nil {
			problem = unreachable(asked.socket, err)
		}
		draw(out, asked.sample, found, problem, asked.socket)

		key, keyErr := readKey(in)
		if keyErr != nil {
			return 0
		}
		decided, ok := interpret(key)
		if !ok {
			continue
		}
		if decided.quit {
			return 0
		}
		if err != nil || found.Pair == nil {
			// Nothing to act on yet: any other key just asks again.
			continue
		}

		after = found.Pair.ID
		if decided.skip {
			continue
		}
		if err := client.Judge(ctx, *found.Pair, decided.label); err != nil {
			draw(out, asked.sample, found, unreachable(asked.socket, err), asked.socket)
			return 1
		}
	}
}

func draw(out io.Writer, sample string, found Next, problem, sock string) {
	fmt.Fprint(out, terminal.HomeClear+frame(sample, found, problem, terminal.Detect(out), sock, time.Now().UTC()))
}

func unreachable(path string, err error) string {
	return fmt.Sprintf("cannot reach the bastion at %s: %v", path, err)
}
