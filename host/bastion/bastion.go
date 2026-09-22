package bastion

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/chrisguidry/agentic-memory/host/claudecode"
	"github.com/chrisguidry/agentic-memory/host/recall"
	"github.com/chrisguidry/agentic-memory/host/repository"
	"github.com/chrisguidry/agentic-memory/host/scope"
	"github.com/chrisguidry/agentic-memory/host/socket"
	"github.com/chrisguidry/agentic-memory/host/transcripts"
)

// Run serves the socket until the context ends or the process is told to stop.
func Run(ctx context.Context, config Config, logger *log.Logger) error {
	if config.Service == "" {
		return errors.New("AGENTIC_MEMORY_SERVICE names no service")
	}

	listener, err := socket.Listen(config.Socket)
	if err != nil {
		return err
	}
	defer listener.Close()

	routes, shipper, err := Routes(config, logger)
	if err != nil {
		return err
	}
	go shipper.Retry(ctx)

	server := &http.Server{
		Handler: routes,
		// The bastion answers one person's own clients over a socket only the
		// kernel lets them reach, so a request has no header timeout to meet.
		ReadHeaderTimeout: 10 * time.Second,
	}
	ctx, stop := signal.NotifyContext(ctx, syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	served := make(chan error, 1)
	go func() { served <- server.Serve(listener) }()
	logger.Printf("listening on %s for %s", config.Socket, config.Service)

	select {
	case err := <-served:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	case <-ctx.Done():
		logger.Printf("stopping")
		closing, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return server.Shutdown(closing)
	}
}

// Routes builds everything the bastion serves, and the shipper behind it.
func Routes(config Config, logger *log.Logger) (http.Handler, *transcripts.Shipper, error) {
	client := &http.Client{Transport: transport()}
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, nil, err
	}
	derive := func(cwd string) (string, string) { return scope.Of(cwd, home) }

	shipper := &transcripts.Shipper{
		Service:       config.Service,
		Authorization: config.Authorization,
		StateDir:      filepath.Join(config.StateDir, claudecode.Harness),
		HTTP:          client,
		Log:           logger,
		Scope:         derive,
		Repositories:  &repository.Reader{},
	}
	hooks := &claudecode.Hooks{
		Recall:   &recall.Client{Service: config.Service, Authorization: config.Authorization, HTTP: client},
		Shipper:  shipper,
		Scope:    derive,
		Machine:  config.Machine,
		Limit:    config.Limit,
		Deadline: config.Deadline,
		Log:      logger,
	}
	onward, err := proxy(config.Service, config.Authorization, client.Transport, logger)
	if err != nil {
		return nil, nil, err
	}

	routes := http.NewServeMux()
	routes.Handle("POST /claude-code/hooks", hooks)
	routes.Handle("POST /transcripts/ship", ship(shipper, config.Machine, logger))
	routes.Handle("/", onward)
	return routes, shipper, nil
}

// ship answers `POST /transcripts/ship` with what the service counted.
// Backfill calls it once per file.
func ship(shipper *transcripts.Shipper, machine string, logger *log.Logger) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var request transcripts.Request
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		if request.Harness == "" || request.Path == "" {
			http.Error(w, "the request names no harness or no path", http.StatusBadRequest)
			return
		}
		if request.Machine == "" {
			request.Machine = machine
		}
		answer, err := shipper.Ship(r.Context(), request)
		if err != nil {
			logger.Printf("could not ship %s: %v", request.Path, err)
			http.Error(w, err.Error(), http.StatusBadGateway)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write(answer)
	})
}
