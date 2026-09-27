package claudecode

import (
	"reflect"
	"testing"

	"github.com/chrisguidry/agentic-memory/host/recall"
)

// The bastion keeps the probe sessions it used last, up to its limit, and a
// session read or handed something counts as used.
func TestHandedKeepsTheSessionsUsedLast(t *testing.T) {
	kept := handed{limit: 2}
	statement := []recall.Statement{{ID: 7}}

	kept.add("probe-a", statement)
	kept.add("probe-b", statement)
	kept.of("probe-a")
	kept.add("probe-c", statement)

	for _, test := range []struct {
		session string
		want    []int64
	}{
		{"probe-a", []int64{7}},
		{"probe-b", nil},
		{"probe-c", []int64{7}},
	} {
		if found := kept.of(test.session); !reflect.DeepEqual(found, test.want) {
			t.Errorf("%s was handed %v, want %v", test.session, found, test.want)
		}
	}
}
