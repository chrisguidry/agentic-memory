// Command agentic-memory holds one person's memory on one machine: the
// bastion that talks to the service, and the hooks every harness runs.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"

	"github.com/chrisguidry/agentic-memory/host/backfill"
	"github.com/chrisguidry/agentic-memory/host/bastion"
	"github.com/chrisguidry/agentic-memory/host/claudecode"
	"github.com/chrisguidry/agentic-memory/host/top"
)

// version is set at build time. A binary built without it says so.
var version = "dev"

const usage = `agentic-memory keeps one person's memory across their coding agents.

  agentic-memory bastion    serve the socket: recall, transcripts, and the
                            proxy to the service
  agentic-memory claude     the Claude Code hook: read the payload on stdin,
                            write what the turn should read to stdout
  agentic-memory backfill   load the sessions a harness already wrote into
                            the service, one file at a time
  agentic-memory top        watch what the memory loop is producing
  agentic-memory version    print the version

The bastion reads AGENTIC_MEMORY_SERVICE, AGENTIC_MEMORY_AUTHORIZATION,
AGENTIC_MEMORY_RECALL_DEADLINE_MS, AGENTIC_MEMORY_RECALL_LIMIT, and
AGENTIC_MEMORY_MACHINE. AGENTIC_MEMORY_SOCKET names the socket for the
bastion and for every client.
`

func main() {
	// Claude Code runs the hook on every turn, so it is dispatched before
	// anything else this program could do. Flag parsing, configuration, and
	// logging all happen after this line.
	if len(os.Args) > 1 && os.Args[1] == "claude" {
		claudecode.Hook(os.Stdin, os.Stdout)
		return
	}

	switch command() {
	case "bastion":
		serve()
	case "backfill":
		fill()
	case "top":
		os.Exit(top.Run(os.Args[2:], os.Stdout))
	case "version":
		fmt.Println(version)
	case "help", "-h", "--help":
		fmt.Print(usage)
	default:
		fmt.Fprint(os.Stderr, usage)
		os.Exit(2)
	}
}

func command() string {
	if len(os.Args) < 2 {
		return ""
	}
	return os.Args[1]
}

func fill() {
	if err := backfill.Run(os.Args[2:], os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func serve() {
	// The journal adds its own timestamp to every line, so the logger adds
	// none.
	logger := log.New(os.Stderr, "", 0)
	config := bastion.Configure()

	flags := flag.NewFlagSet("bastion", flag.ExitOnError)
	flags.StringVar(&config.Socket, "socket", config.Socket, "the socket to serve on")
	flags.Parse(os.Args[2:])

	if err := bastion.Run(context.Background(), config, logger); err != nil {
		logger.Printf("%v", err)
		os.Exit(1)
	}
}
