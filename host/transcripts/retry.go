package transcripts

import (
	"context"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// The backoff a file waits through after a failure. It starts short, because
// the common failure is a service restarting, and it caps well inside the time
// a person leaves between sessions.
const (
	FirstWait   = 5 * time.Second
	LongestWait = 5 * time.Minute
)

// Grace is how long a transcript may not exist before the bastion drops it.
// Claude Code writes the first prompt into the transcript a moment after it
// fires the event that names the file, so a file that has not appeared in two
// minutes was never going to. The next event in a live session ships the file
// again whether or not it was dropped.
const Grace = 2 * time.Minute

// behind is what the bastion keeps about a file the service has not taken: the
// request to offer again, and when. It is written into the file's offset
// record, so a restarted bastion offers the file again.
type behind struct {
	Request Request       `json:"request"`
	Wait    time.Duration `json:"wait"`
	Due     time.Time     `json:"due"`
	// Missing is when the bastion first found the file not written, and is zero
	// while the file exists.
	Missing time.Time `json:"missing,omitzero"`
}

// attempt is a file that is behind, held in memory for the ticker to read.
type attempt struct {
	mu sync.Mutex
	behind
}

// failed records why a file did not ship, and whether and when to offer it
// again. Ship calls it while it holds the file's lock.
func (s *Shipper) failed(request Request, err error) {
	name := filepath.Base(request.Path)
	now := s.now()
	stored, _ := s.pending.LoadOrStore(request.Path, &attempt{})
	found := stored.(*attempt)
	found.mu.Lock()
	defer found.mu.Unlock()
	found.Request = request

	switch causeOf(err) {
	case gone:
		s.drop(request.Path)
		s.logf("%s: gone, dropped: %v", name, err)
		return
	case notWrittenYet:
		if found.Missing.IsZero() {
			found.Missing = now
			s.logf("%s: not written yet, waiting up to %s", name, Grace)
		} else if now.Sub(found.Missing) >= Grace {
			s.drop(request.Path)
			s.logf("%s: not written after %s, dropped", name, Grace)
			return
		}
	default:
		found.Missing = time.Time{}
	}

	found.Wait = longer(found.Wait)
	found.Due = now.Add(found.Wait)
	if found.Missing.IsZero() {
		s.logf("%s: behind, retrying in %s: %v", name, found.Wait, err)
	} else if end := found.Missing.Add(Grace); found.Due.After(end) {
		// The last look lands on the end of the grace period, so a file that
		// never appears is dropped at the grace period and not a wait later.
		found.Due = end
	}
	s.keep(request.Path, found.behind)
}

func longer(wait time.Duration) time.Duration {
	if wait == 0 {
		return FirstWait
	}
	return min(wait*2, LongestWait)
}

func (s *Shipper) delivered(path string) {
	if _, was := s.pending.LoadAndDelete(path); was {
		s.forget(path)
	}
}

func (s *Shipper) drop(path string) {
	s.pending.Delete(path)
	s.forget(path)
}

// keep writes a file that is behind into its offset record. The in-memory
// retry goes on without it, so a failure here costs only the resume after a
// restart.
func (s *Shipper) keep(path string, found behind) {
	name := offsetFile(s.StateDir, path)
	record := readOffset(name)
	record.Behind = &found
	if err := writeOffset(name, record); err != nil {
		s.logf("%s: could not record it as behind: %v", filepath.Base(path), err)
	}
}

// forget clears a file from its offset record. A record with no offset holds
// nothing else worth keeping, so it is removed.
func (s *Shipper) forget(path string) {
	name := offsetFile(s.StateDir, path)
	record := readOffset(name)
	record.Behind = nil
	var err error
	if record.Offset == 0 {
		err = os.Remove(name)
	} else {
		err = writeOffset(name, record)
	}
	if err != nil && !errors.Is(err, fs.ErrNotExist) {
		s.logf("%s: could not clear it as behind: %v", filepath.Base(path), err)
	}
}

// Resume reads the files a previous bastion left behind from the offset
// records, so the ticker offers them to the service again. Each keeps its
// wait, so one whose wait passed while no bastion ran is offered at once.
func (s *Shipper) Resume() {
	names, _ := filepath.Glob(filepath.Join(s.StateDir, "*.json"))
	resumed := 0
	for _, name := range names {
		record := readOffset(name)
		if record.Behind == nil {
			continue
		}
		s.pending.Store(record.Behind.Request.Path, &attempt{behind: *record.Behind})
		resumed++
	}
	if resumed > 0 {
		s.logf("resumed %d files behind", resumed)
	}
}

// Behind is how many files the service has not taken yet.
func (s *Shipper) Behind() int {
	found := 0
	s.pending.Range(func(any, any) bool {
		found++
		return true
	})
	return found
}

// RetryDue ships every file that is behind and whose wait has passed. A file
// that fails again gets a longer wait, or is dropped if it is gone.
func (s *Shipper) RetryDue(ctx context.Context, now time.Time) {
	var due []Request
	s.pending.Range(func(_, stored any) bool {
		found := stored.(*attempt)
		found.mu.Lock()
		if !found.Due.After(now) {
			due = append(due, found.Request)
		}
		found.mu.Unlock()
		return true
	})
	for _, request := range due {
		s.Ship(ctx, request)
	}
}

// Retry offers files that are behind to the service until it takes them, for
// as long as the bastion runs.
func (s *Shipper) Retry(ctx context.Context) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			s.RetryDue(ctx, s.now())
		}
	}
}

func (s *Shipper) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now()
}
