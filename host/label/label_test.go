package label

import (
	"context"
	"errors"
	"strings"
	"testing"
)

func TestInterpret(t *testing.T) {
	cases := []struct {
		key  byte
		want judgment
		ok   bool
	}{
		{'g', judgment{label: "good"}, true},
		{'n', judgment{label: "noise"}, true},
		{'w', judgment{label: "wrong"}, true},
		{'s', judgment{skip: true}, true},
		{'c', judgment{context: true}, true},
		{'q', judgment{quit: true}, true},
		{3, judgment{quit: true}, true},
		{'x', judgment{}, false},
		{' ', judgment{}, false},
	}
	for _, found := range cases {
		got, ok := interpret(found.key)
		if got != found.want || ok != found.ok {
			t.Errorf("interpret(%q) = %+v, %v, want %+v, %v", found.key, got, ok, found.want, found.ok)
		}
	}
}

// fakeService stands in for the bastion's socket: it answers a fixed sequence
// of pairs and remembers what it was asked and told.
type fakeService struct {
	pairs   []Pair
	next    int
	asked   []int
	judged  []string
	failing bool

	// exchanges answers Context by page, in the order browseContext asks for
	// them; a page past the end answers nil, the way the real service does
	// once paging runs out.
	exchanges    []*Exchange
	contextAsked []int
}

func (f *fakeService) Next(_ context.Context, _ string, after int) (Next, error) {
	if f.failing {
		return Next{}, errors.New("refused")
	}
	f.asked = append(f.asked, after)
	if f.next >= len(f.pairs) {
		return Next{Done: len(f.pairs), Total: len(f.pairs), Pair: nil}, nil
	}
	pair := f.pairs[f.next]
	return Next{Done: f.next, Total: len(f.pairs), Pair: &pair}, nil
}

func (f *fakeService) Judge(_ context.Context, pair Pair, label string) error {
	f.judged = append(f.judged, label)
	f.next++
	return nil
}

func (f *fakeService) Context(_ context.Context, _, _ string, page int) (*Exchange, error) {
	if f.failing {
		return nil, errors.New("refused")
	}
	f.contextAsked = append(f.contextAsked, page)
	if page >= len(f.exchanges) {
		return nil, nil
	}
	return f.exchanges[page], nil
}

func testPairs(n int) []Pair {
	pairs := make([]Pair, n)
	for i := range pairs {
		pairs[i] = Pair{ID: i + 1, SessionID: "s1", EntryID: "e1", Statement: "a rule"}
	}
	return pairs
}

func TestALabelKeyJudgesAndMovesOn(t *testing.T) {
	service := &fakeService{pairs: testPairs(2)}
	var out strings.Builder
	status := loop(options{sample: "week-1", socket: "sock"}, service, strings.NewReader("gnq"), &out)
	if status != 0 {
		t.Errorf("loop exited %d, want 0", status)
	}
	if got := service.judged; len(got) != 2 || got[0] != "good" || got[1] != "noise" {
		t.Errorf("judged %v, want [good noise]", got)
	}
}

func TestASkipMovesOnWithoutJudging(t *testing.T) {
	service := &fakeService{pairs: testPairs(2)}
	var out strings.Builder
	loop(options{sample: "week-1", socket: "sock"}, service, strings.NewReader("sgq"), &out)
	if len(service.judged) != 1 || service.judged[0] != "good" {
		t.Errorf("judged %v, want [good]", service.judged)
	}
	// The skip asked again with the same pair's id, and the judged pair with
	// the id after it: skipping never repeats what was already shown.
	if len(service.asked) < 2 || service.asked[0] != 0 || service.asked[1] != 1 {
		t.Errorf("asked after %v, want it to move past the skipped pair", service.asked)
	}
}

func TestAQuitStopsBeforeAskingAgain(t *testing.T) {
	service := &fakeService{pairs: testPairs(3)}
	var out strings.Builder
	status := loop(options{sample: "week-1", socket: "sock"}, service, strings.NewReader("q"), &out)
	if status != 0 {
		t.Errorf("loop exited %d, want 0", status)
	}
	if len(service.judged) != 0 {
		t.Errorf("a quit before any label still judged %v", service.judged)
	}
}

func TestAnUnknownKeyIsIgnored(t *testing.T) {
	service := &fakeService{pairs: testPairs(1)}
	var out strings.Builder
	loop(options{sample: "week-1", socket: "sock"}, service, strings.NewReader("x g"), &out)
	if len(service.judged) != 1 || service.judged[0] != "good" {
		t.Errorf("judged %v, want [good]", service.judged)
	}
}

func TestRunningOutOfInputStopsTheLoop(t *testing.T) {
	service := &fakeService{pairs: testPairs(1)}
	var out strings.Builder
	status := loop(options{sample: "week-1", socket: "sock"}, service, strings.NewReader(""), &out)
	if status != 0 {
		t.Errorf("loop exited %d, want 0", status)
	}
}

func TestAKeyAfterTheSampleIsDoneAsksAgainRatherThanJudging(t *testing.T) {
	service := &fakeService{pairs: testPairs(0)}
	var out strings.Builder
	loop(options{sample: "week-1", socket: "sock"}, service, strings.NewReader("gq"), &out)
	if len(service.judged) != 0 {
		t.Errorf("a finished sample was judged: %v", service.judged)
	}
}

func TestAKeyWhileTheServiceIsUnreachableAsksAgainRatherThanCrashing(t *testing.T) {
	service := &fakeService{pairs: testPairs(1), failing: true}
	var out strings.Builder
	status := loop(options{sample: "week-1", socket: "sock"}, service, strings.NewReader("gq"), &out)
	if status != 0 {
		t.Errorf("loop exited %d, want 0", status)
	}
	if len(service.judged) != 0 {
		t.Errorf("judged while unreachable: %v", service.judged)
	}
}

func TestCOpensContextAndAnyOtherKeyReturnsToThePair(t *testing.T) {
	service := &fakeService{
		pairs: testPairs(1),
		exchanges: []*Exchange{
			{EntryID: "e0", Prompt: "an earlier question", Replies: []string{"an earlier answer"}},
		},
	}
	var out strings.Builder
	status := loop(options{sample: "week-1", socket: "sock"}, service, strings.NewReader("c gq"), &out)
	if status != 0 {
		t.Errorf("loop exited %d, want 0", status)
	}
	if got := service.contextAsked; len(got) != 1 || got[0] != 0 {
		t.Errorf("context asked %v, want [0]", got)
	}
	// The space returned to the pair, and the g after it is what judged.
	if len(service.judged) != 1 || service.judged[0] != "good" {
		t.Errorf("judged %v, want [good]", service.judged)
	}
}

func TestRepeatedCPagesToOlderExchanges(t *testing.T) {
	service := &fakeService{
		pairs: testPairs(1),
		exchanges: []*Exchange{
			{EntryID: "e1", Prompt: "newer question"},
			{EntryID: "e0", Prompt: "older question"},
		},
	}
	var out strings.Builder
	loop(options{sample: "week-1", socket: "sock"}, service, strings.NewReader("cc q"), &out)
	if got := service.contextAsked; len(got) != 2 || got[0] != 0 || got[1] != 1 {
		t.Errorf("context asked %v, want [0 1]", got)
	}
}

func TestCPastTheLastExchangeFallsBackToThePairRatherThanPagingFurther(t *testing.T) {
	service := &fakeService{
		pairs:     testPairs(1),
		exchanges: []*Exchange{{EntryID: "e0", Prompt: "only earlier question"}},
	}
	var out strings.Builder
	// c shows the one exchange there is; a second c finds nothing older, so
	// a third c does not count as "older still" and instead falls back to
	// the pair, where g judges it.
	loop(options{sample: "week-1", socket: "sock"}, service, strings.NewReader("ccxgq"), &out)
	if len(service.judged) != 1 || service.judged[0] != "good" {
		t.Errorf("judged %v, want [good]", service.judged)
	}
}

func TestAQuitInsideContextQuitsTheProgram(t *testing.T) {
	service := &fakeService{
		pairs:     testPairs(1),
		exchanges: []*Exchange{{EntryID: "e0", Prompt: "an earlier question"}},
	}
	var out strings.Builder
	status := loop(options{sample: "week-1", socket: "sock"}, service, strings.NewReader("cq"), &out)
	if status != 0 {
		t.Errorf("loop exited %d, want 0", status)
	}
	if len(service.judged) != 0 {
		t.Errorf("a quit from context still judged: %v", service.judged)
	}
}
