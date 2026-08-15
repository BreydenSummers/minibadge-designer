#!/usr/bin/env bash
# redproof.sh -- prove a test goes red for the right reason, and say so in the
# commit message using words the run actually produced.
#
# A `Red-proof:` trailer that only proves somebody typed it is theatre. This
# script emits the trailer *only* when it has watched, in one uninterrupted
# sequence:
#
#     green -> (mutate) -> red with an assertion from your test -> (restore)
#     -> green again, same number of tests collected
#
# and it pastes the observed failure line into the trailer. If any step fails
# it prints why and exits non-zero, and there is no trailer to copy.
#
# Three traps, all reproduced in round 1, are handled here rather than left to
# the operator:
#
#   1. .pyc staleness. CPython validates bytecode on (source mtime in whole
#      seconds, source size). A plausible mutation is often the SAME byte
#      length as the original, so an edit-run-revert-run cycle finishing inside
#      one second reruns the *mutant's* bytecode after the restore and reports
#      a false red. Every run here is PYTHONDONTWRITEBYTECODE=1 -B with
#      __pycache__ removed first.
#   2. --tb=line hides the word AssertionError entirely: a rewritten assertion
#      prints `assert 7500.0 == 10000.0` and nothing else. Everything here runs
#      --tb=short, and an import/collection/TypeError red is classified as
#      NOT VERIFIED, not as success.
#   3. A green run does not by itself convict the test. If --probe is given,
#      the probe is run TWICE against the pristine tree first (a probe that is
#      not deterministic on its own can never show that the *mutation* moved
#      it), and only then pristine-vs-mutant.
#
# Four forgeries found by the round-3 adversary are closed here, and each is
# marked in place below so a future editor can see what the code is for:
#
#   F5a  the "do not break the test suite" guard was a string match on the
#        path the operator typed. `--file Tests/test_pcb.py` (case-insensitive
#        APFS) and a symlink from minibadge_designer/ into tests/ both earned a
#        VERIFIED trailer for mutating the test's own assertion. Paths are now
#        resolved -- symlinks followed, on-disk case recovered -- and compared
#        by inode against the repo's tests directory.  See resolve_target().
#   F5b  classify() took the longest message anywhere in the output and paired
#        it with the last frame anywhere in the output, which on any multi-test
#        run is a different failure. Message and location now come from the
#        same failure record.
#   F5c  --probe 'date +%s%N' earned "the mutant is observable". The probe must
#        now prove itself deterministic on the pristine tree before its
#        difference is allowed to mean anything.
#   F5e  a concurrent edit to the target by another process was silently
#        reverted and reported "byte-identical". The restore now refuses unless
#        the file on disk is still exactly the mutant this run wrote.
#
# This script is TRACKED, and deliberately so (decision D15). It used to live
# under .claude/skills/writing-tests/scripts/, which is gitignored, so the
# ritual that decides whether a test is real could be weakened without a single
# line appearing in `git status`. It lives beside the suite it verifies now.
#
# Usage
#   tests/redproof.sh --test <nodeid> --file <path> --old <text> --new <text>
#               [--probe <shell command>] [--repo <dir>] [--python <exe>]
#               [--trailer-only]
#
# Example
#   tests/redproof.sh \
#     --test tests/test_pcb.py::test_via_annular_ring_meets_the_fab_minimum \
#     --file minibadge_designer/pcb.py \
#     --old 'VIA_SIZE, VIA_DRILL = 0.7, 0.3' \
#     --new 'VIA_SIZE, VIA_DRILL = 0.7, 0.5' \
#     --probe '.venv/bin/python -c "from minibadge_designer import pcb; print(pcb.generate_pcb(pcb.BadgeSpec(leds=[pcb.Led(10,10,\"red\")])))"'
#
# --old must match exactly once in --file. An anchor that matches zero times or
# twice is rejected: a mutation that silently fails to apply produces a fake
# "the test caught nothing".
#
# Exit codes: 0 verified (trailer printed) | 1 not verified | 2 usage/setup error.

set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# tests/redproof.sh -> the repo root is one level up.
REPO_DEFAULT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

TEST=""; FILE=""; OLD=""; NEW=""; PROBE=""
REPO="$REPO_DEFAULT"; PYTHON=""; TRAILER_ONLY=0

die() { printf 'redproof: %s\n' "$*" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --test)         TEST="${2:-}"; shift 2 ;;
    --file)         FILE="${2:-}"; shift 2 ;;
    --old)          OLD="${2:-}"; shift 2 ;;
    --new)          NEW="${2:-}"; shift 2 ;;
    --probe)        PROBE="${2:-}"; shift 2 ;;
    --repo)         REPO="${2:-}"; shift 2 ;;
    --python)       PYTHON="${2:-}"; shift 2 ;;
    --trailer-only) TRAILER_ONLY=1; shift ;;
    -h|--help)      sed -n '2,76p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)              die "unknown argument: $1" ;;
  esac
done

[ -n "$TEST" ] || die "--test <nodeid> is required"
[ -n "$FILE" ] || die "--file <path to the production source you will break> is required"
[ -n "$OLD" ]  || die "--old <exact text to replace> is required"
[ -n "$NEW" ]  || die "--new <replacement text> is required"
[ "$OLD" != "$NEW" ] || die "--old and --new are identical; that mutates nothing"

cd "$REPO" 2>/dev/null || die "no such repo: $REPO"
REPO="$(pwd -P)"

if [ -z "$PYTHON" ]; then
  if [ -x "$REPO/.venv/bin/python" ]; then PYTHON="$REPO/.venv/bin/python"; else PYTHON="python3"; fi
fi
"$PYTHON" -c 'import pytest' 2>/dev/null || die "$PYTHON cannot import pytest"

# --- resolve --file to a real location (forgery F5a) ----------------------
# This guard used to be `case "$FILE" in tests/*|*/tests/*)`, a string match on
# what the operator typed, and it was walked past twice:
#
#   (i)  --file 'Tests/test_pcb.py'  -- the glob is case-sensitive, APFS is not,
#        so the shell opened tests/test_pcb.py and the guard saw "Tests/...".
#   (ii) ln -s ../tests/test_pcb.py minibadge_designer/decoy.py, then
#        --file minibadge_designer/decoy.py -- a production-looking path that
#        writes into the test suite. The trailer then named decoy.py while the
#        failure location inside the SAME trailer said tests/test_pcb.py:38.
#
# Both signed a VERIFIED trailer for a run in which zero production code was
# changed. So: follow symlinks, recover the real on-disk spelling of every
# component, and decide by inode (os.path.samefile) whether the real location
# lives under the repo's tests/ directory. Everything downstream -- the
# mutation, the snapshot, the restore, the trailer -- uses the RESOLVED path,
# so the trailer can no longer name one file while quoting another.
PATHS="$(RP_REPO="$REPO" RP_FILE="$FILE" "$PYTHON" - <<'PY'
import os

def truecase(path):
    """Real path, with each component spelled the way the disk spells it.

    os.path.realpath() follows symlinks but does NOT fix case: APFS is
    case-insensitive and case-preserving, so realpath('Tests/x.py') comes back
    as 'Tests/x.py' even though it opened 'tests/x.py'.
    """
    parts, cur = [], os.path.realpath(path)
    while True:
        head, tail = os.path.split(cur)
        if not tail:
            parts.append(head)
            break
        try:
            names = os.listdir(head)
        except OSError:
            names = []
        if tail not in names:
            hit = [n for n in names if n.lower() == tail.lower()]
            if len(hit) == 1:
                tail = hit[0]
        parts.append(tail)
        cur = head
    return os.path.join(*reversed(parts))

def ancestors(p):
    out = []
    while True:
        parent = os.path.dirname(p)
        if parent == p:
            return out
        out.append(parent)
        p = parent

def same(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False

repo = truecase(os.environ["RP_REPO"])
target = truecase(os.path.join(repo, os.environ["RP_FILE"]))
anc = ancestors(target)

in_repo = any(same(a, repo) for a in anc)
testdir = os.path.join(repo, "tests")
in_tests = (
    (os.path.isdir(testdir) and any(same(a, testdir) for a in anc))
    or any(os.path.basename(a).lower() == "tests" for a in anc)
    or os.path.basename(target).lower().startswith("test_")
    or os.path.basename(target).lower() in ("conftest.py", "invariants.py", "hostile.py")
)
print("TARGET=" + target)
print("REL=" + (os.path.relpath(target, repo) if in_repo else target))
print("IN_REPO=%d" % int(in_repo))
print("IN_TESTS=%d" % int(in_tests))
PY
)" || die "cannot resolve --file $FILE"

TARGET=""; FILE_REL=""; IN_REPO=0; IN_TESTS=0
while IFS='=' read -r _k _v; do
  case "$_k" in
    TARGET)   TARGET="$_v" ;;
    REL)      FILE_REL="$_v" ;;
    IN_REPO)  IN_REPO="$_v" ;;
    IN_TESTS) IN_TESTS="$_v" ;;
  esac
done <<< "$PATHS"

[ -n "$TARGET" ] || die "cannot resolve --file $FILE"
[ -f "$TARGET" ] || die "no such file: $FILE
     (resolved to $TARGET)"
[ "$IN_REPO" = "1" ] || die "--file resolves outside the repo:
     typed    $FILE
     resolves $TARGET
     repo     $REPO
     Mutating a file the repo does not contain proves nothing about it."
[ "$IN_TESTS" = "0" ] || die "--file resolves into the test suite:
     typed    $FILE
     resolves $FILE_REL
     Break the PRODUCTION code the test claims to cover; editing the test to
     make it fail proves nothing. (Case and symlinks are resolved before this
     check precisely because both were used to walk past it.)"
if [ "$FILE_REL" != "$FILE" ]; then
  printf 'redproof: note: --file %s resolves to %s; using the resolved path.\n' \
         "$FILE" "$FILE_REL" >&2
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/redproof.XXXXXX")" || die "mktemp failed"
SNAP="$WORK/pristine"
RESTORED=0
SHA0=""
SHA_MUT=""

purge_bytecode() {
  # Trap 1. Belt and braces: we also never write new bytecode (-B).
  find "$REPO/minibadge_designer" "$REPO/tests" -name __pycache__ -type d \
       -exec rm -rf {} + 2>/dev/null
  true
}

sha() { "$PYTHON" -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1" 2>/dev/null; }

# whose_bytes -> PRISTINE | MUTANT | TORN | FOREIGN | MISSING
#
# The whole restore decision rests on this, so it is a comparison against the
# two files we hold, not a guess. TORN means the target is a strict prefix of
# something we wrote -- a write of ours interrupted by a signal, which IS ours
# to undo. FOREIGN means it is neither, and then somebody else owns those bytes
# and we must not touch them.
whose_bytes() {
  RP_T="$TARGET" RP_P="$SNAP" RP_M="$WORK/mutant" "$PYTHON" - <<'PY' 2>/dev/null || echo MISSING
import os
def rd(p):
    try:
        with open(p, "rb") as fh:
            return fh.read()
    except OSError:
        return None
cur = rd(os.environ["RP_T"])
pris = rd(os.environ["RP_P"])
mut  = rd(os.environ["RP_M"])
if cur is None:
    print("MISSING")
elif pris is not None and cur == pris:
    print("PRISTINE")
elif mut is not None and cur == mut:
    print("MUTANT")
elif any(w is not None and len(cur) < len(w) and w.startswith(cur) for w in (mut, pris)):
    print("TORN")
else:
    print("FOREIGN")
PY
}

# --- restoration ----------------------------------------------------------
# Runs on normal exit AND on Ctrl-C, SIGTERM and SIGHUP.
#
# Forgery F5e: this used to `cat "$SNAP" > "$FILE"` unconditionally and then
# compare that write against the snapshot -- a tautology that always printed
# "byte-identical". A coworker's edit landing between the snapshot and the
# restore was destroyed without a word. (Reproduced: a second process appended
# a line while the mutant was on disk; redproof reported byte-identical and the
# line was gone.) Now the file must still be EXACTLY the mutant this run wrote
# before anything is overwritten. If it is not, we keep our hands off it and
# leave the pristine copy behind for the human to merge.
restore() {
  local rc=$?
  if [ -f "$SNAP" ] && [ "$RESTORED" -eq 0 ]; then
    local owner; owner="$(whose_bytes)"
    if [ "$owner" = "PRISTINE" ]; then
      # Never mutated (or already put back). Nothing to write -- which is also
      # why a read-only target no longer produces a bogus "source restored".
      RESTORED=1
    elif [ "$owner" = "MUTANT" ] || [ "$owner" = "TORN" ]; then
      cat "$SNAP" > "$TARGET"
      purge_bytecode
      if cmp -s "$SNAP" "$TARGET"; then
        RESTORED=1
        [ "$rc" -ne 0 ] && printf 'redproof: source restored (%s).\n' "$FILE_REL" >&2
      else
        printf 'redproof: *** RESTORE FAILED for %s. Pristine copy kept at %s ***\n' \
               "$FILE_REL" "$SNAP" >&2
        trap - EXIT INT TERM HUP; exit 2
      fi
    else
      printf 'redproof: *** %s CHANGED UNDERNEATH THIS RUN -- NOT RESTORING ***\n' "$FILE_REL" >&2
      printf 'redproof: the file on disk is neither the pristine copy nor the mutant\n' >&2
      printf 'redproof: this run wrote, so somebody else edited it while we ran.\n' >&2
      printf 'redproof: Overwriting it would destroy their work, so we have not.\n' >&2
      printf 'redproof:   pristine copy : %s\n' "$SNAP" >&2
      printf 'redproof:   mutation to undo by hand: `%s` -> `%s` at %s:%s\n' \
             "$OLD" "$NEW" "$FILE_REL" "${MUT_LINE:-?}" >&2
      printf 'redproof: This verification is void. Merge, then run it again.\n' >&2
      trap - EXIT INT TERM HUP; exit 2
    fi
  fi
  [ -d "$WORK" ] && [ "$RESTORED" -eq 1 ] && rm -rf "$WORK"
  return $rc
}
trap 'restore' EXIT
trap 'trap - EXIT; restore; exit 130' INT TERM HUP

# --- pytest driver --------------------------------------------------------
# --tb=short is trap 2 and is not negotiable. -p no:cacheprovider keeps
# .pytest_cache out of the way so a concurrent run cannot perturb ordering.
run_pytest() {           # run_pytest <label>  -> writes $WORK/<label>.txt, echoes exit code
  local label="$1"
  purge_bytecode
  PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX= \
    "$PYTHON" -B -m pytest -q --tb=short -p no:cacheprovider "$TEST" \
    > "$WORK/$label.txt" 2>&1
  echo $?
}

classify() {             # classify <label> -> "VERDICT|collected|evidence"
  "$PYTHON" - "$WORK/$1.txt" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()

counts = {}
for n, word in re.findall(r"(\d+) (passed|failed|error|errors|skipped|xfailed|deselected)", text):
    counts[word.rstrip("s")] = counts.get(word.rstrip("s"), 0) + int(n)
collected = counts.get("passed", 0) + counts.get("failed", 0) + counts.get("error", 0)

# The trailing `short test summary info` block repeats every FAILED line with a
# truncated message and no frame. Parsing it would double-count and mispair, so
# failure parsing stops there. The counts above still come from the full text.
body = re.split(r"^=+ short test summary info =+", text, maxsplit=1, flags=re.M)[0]

# --- forgery F5b: pair the message with ITS OWN location ------------------
# This used to be two independent picks:
#     evidence = max(with_msg or assert_lines, key=len)   # longest message anywhere
#     where    = re.findall(...)[-1]                      # last frame anywhere
# On any run with more than one failure -- a file, a class, a directory, or a
# parametrized nodeid such as tests/test_board_invariants.py's 26 params -- the
# trailer then quoted one test's assertion next to another test's line number.
# Reproduced on a two-failure run: the message from test_alpha (asserting at
# :2) was stamped [twofail.py:8], which is test_zeta.
#
# Instead: walk the failure section in order, remember the most recent frame
# line, and attach it to each `E` line as that record's own location. A `___
# test_name ___` banner resets the frame so nothing is inherited across
# failures.
FRAME  = re.compile(r"^(\S+\.py):(\d+): (?:in \S+|\w*(?:Error|Exception)\b.*)$")
EMARK  = re.compile(r"^E\s+(.*)$")
BANNER = re.compile(r"^(?:_{5,}|={5,})")

records, frame = [], ""
for raw in body.splitlines():
    line = raw.rstrip()
    if BANNER.match(line.strip()):
        frame = ""                       # new failure block: inherit nothing
        continue
    m = FRAME.match(line.strip())
    if m:
        frame = f"{m.group(1)}:{m.group(2)}"
        continue
    e = EMARK.match(line.strip())
    if not e:
        continue
    msg = e.group(1).strip()
    if re.match(r"(assert\b|AssertionError\b)", msg):
        records.append(("assert", msg, frame))
    elif re.match(r"\w*(Error|Exception)\b", msg):
        records.append(("error", msg, frame))

asserts = [r for r in records if r[0] == "assert"]
errors  = [r for r in records if r[0] == "error"]

broken = bool(re.search(r"errors during collection|INTERNALERROR|"
                        r"ERROR collecting|no tests ran|"
                        r"ModuleNotFoundError|ImportError", text))

def render(rec, room=110):
    """`<message> [<file>:<line>]`, trimming the MESSAGE if anything must go.

    The location is the half that pairs the two, so it is never what gets cut:
    an earlier flat `[:110]` on the joined string produced evidence ending
    `... finished board [te`, which is a location that identifies nothing.
    """
    _, msg, where = rec
    msg = re.sub(r"\s+", " ", msg).strip()
    if len(where) > 45:                     # a site-packages frame, usually
        where = ".../" + "/".join(where.split("/")[-2:])
    tag = f" [{where}]" if where else ""
    keep = max(40, room - len(tag))
    if len(msg) > keep:
        msg = msg[: keep - 1] + "…"
    return msg + tag

if counts.get("failed", 0) == 0 and counts.get("error", 0) == 0 and collected:
    verdict = "GREEN"
    evidence = f"{collected} collected, all passed"
elif broken or counts.get("error", 0):
    verdict = "BROKEN"
    evidence = render(errors[0]) if errors else "collection or import error"
elif asserts:
    verdict = "RED_BY_ASSERTION"
    # `assert None` (a re.search that missed) says nothing; `assert 0.15 >= 0.3`
    # says everything. Take the record that carries the most information, and
    # prefer one with an explicit message -- then use THAT record's location.
    with_msg = [r for r in asserts if re.match(r"AssertionError: \S", r[1])]
    evidence = render(max(with_msg or asserts, key=lambda r: len(r[1])))
elif errors:
    verdict = "RED_BY_ERROR"
    evidence = render(errors[0])
else:
    verdict = "RED_UNCLASSIFIED"
    evidence = "failed, but no `E` line found -- did you run with --tb=short?"

evidence = re.sub(r"\s+", " ", evidence)[:140]
print(f"{verdict}|{collected}|{evidence}")
PY
}

say() { [ "$TRAILER_ONLY" -eq 1 ] || printf '%s\n' "$*"; }
show() { [ "$TRAILER_ONLY" -eq 1 ] || sed 's/^/    /' "$WORK/$1.txt" >&2; }

# --- 0. interpreter sanity ------------------------------------------------
SRC_SEEN="$(cd "$REPO" && PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -B -c \
  'import minibadge_designer,sys;print(minibadge_designer.__file__)' 2>/dev/null)"
if [ -z "$SRC_SEEN" ]; then
  die "cannot import minibadge_designer with $PYTHON"
fi
SRC_OK="$(RP_SRC="$SRC_SEEN" RP_REPO="$REPO" "$PYTHON" - <<'PY'
import os
src, repo = os.path.realpath(os.environ["RP_SRC"]), os.path.realpath(os.environ["RP_REPO"])
p = os.path.dirname(src)
while True:
    try:
        if os.path.samefile(p, repo):
            print("1"); break
    except OSError:
        pass
    parent = os.path.dirname(p)
    if parent == p:
        print("0"); break
    p = parent
PY
)"
[ "$SRC_OK" = "1" ] || die "wrong source tree: $PYTHON imports minibadge_designer from
     $SRC_SEEN
     but --repo is $REPO. Fix pythonpath before drawing any conclusion --
     round 1 lost a whole pass red-teaming a checkout nobody runs."

say "redproof: repo   $REPO"
say "redproof: source $SRC_SEEN"
say "redproof: test   $TEST"
say "redproof: mutate $FILE_REL"
say "             -   $OLD"
say "             +   $NEW"
say ""

# --- 1. baseline must be green -------------------------------------------
RC=$(run_pytest baseline)
IFS='|' read -r V0 N0 E0 <<< "$(classify baseline)"
if [ "$V0" != "GREEN" ]; then
  say "NOT VERIFIED: the test is not green before the mutation ($V0: $E0)."
  say "A test that has never passed is not yet a test."
  show baseline
  exit 1
fi
say "  1. baseline      GREEN   ($N0 collected)"

# --- 2. snapshot ----------------------------------------------------------
cat "$TARGET" > "$SNAP" || die "cannot snapshot $FILE_REL"
SHA0="$(sha "$SNAP")"
# The snapshot outlives a SIGKILL, which no trap can catch. Print where it is,
# so `cp <that> <file>` is the recovery even if the process is shot in the head.
say "  2. snapshot      sha256 ${SHA0:0:16}…  ($SNAP)"

# --- 3. is the probe deterministic AT ALL? (forgery F5c) ------------------
# The probe used to be run once pristine, once mutated, and any difference was
# stamped "the mutant is observable". `--probe 'date +%s%N'` therefore earned
# that stamp from a clock. Anything whose output moves for reasons unrelated to
# the mutation -- a timestamp, a PID, a tmp path, a set iteration order, a
# random seed, a `generated_at` field in a GLB (decision D3, measured twice) --
# defeats the check completely.
#
# So the probe has to prove itself first: two runs against the UNMUTATED tree,
# byte-identical. Only then does a later difference carry the meaning "the
# mutation moved it".
if [ -n "$PROBE" ]; then
  ( cd "$REPO" && eval "$PROBE" ) > "$WORK/probe-a.txt" 2>&1
  purge_bytecode
  ( cd "$REPO" && eval "$PROBE" ) > "$WORK/probe-b.txt" 2>&1
  if ! cmp -s "$WORK/probe-a.txt" "$WORK/probe-b.txt"; then
    say ""
    say "NOT VERIFIED: the probe is not deterministic."
    say ""
    say "Two runs of the probe against the PRISTINE, unmutated tree already"
    say "differ. Nothing has been changed yet. A probe like that can never show"
    say "that the *mutation* moved anything -- 'the output differs' would be"
    say "true of 'date +%s%N' too, and the observability stamp would then be"
    say "issued by a clock rather than by the code you edited."
    say ""
    say "First difference between the two pristine runs:"
    diff "$WORK/probe-a.txt" "$WORK/probe-b.txt" 2>/dev/null | head -6 | sed 's/^/    /'
    say ""
    say "Make the probe deterministic: print measured numbers, not timestamps,"
    say "paths, ids or hashes. Sort anything set-derived. Strip the fields that"
    say "are known non-deterministic (a GLB's asset.extras.generated_at was"
    say "measured to change on every export while every number stayed equal)."
    exit 1
  fi
  say "  3. probe         deterministic on the pristine tree (2 runs, byte-identical)"
fi

# --- 4. apply the mutation, exactly once ----------------------------------
# The mutant is composed into $WORK/mutant FIRST and only then copied onto the
# target. That ordering is what makes the F5e ownership test answerable at every
# instant: before the copy the target is PRISTINE, after it is MUTANT, and a
# copy interrupted by a signal leaves a strict prefix of MUTANT, which
# whose_bytes() calls TORN and still restores. Composing in place instead left a
# window -- measured -- in which a SIGTERM landed while the mutant bytes were on
# disk but nothing yet knew what they were, and the restore refused its own work.
APPLY_OUT="$(RP_FILE="$TARGET" RP_OLD="$OLD" RP_NEW="$NEW" RP_OUT="$WORK/mutant" "$PYTHON" - <<'PY'
import os, sys
path, old, new = os.environ["RP_FILE"], os.environ["RP_OLD"], os.environ["RP_NEW"]
src = open(path, encoding="utf-8").read()
n = src.count(old)
if n != 1:
    print(f"ANCHOR:{n}"); sys.exit(1)
if not os.access(path, os.W_OK):
    # Reported precisely, because it used to fall through into the anchor
    # branch and tell the operator "--old appears  times; anchor it uniquely".
    print("ERROR:the file is not writable (mode is read-only)"); sys.exit(1)
line = src[: src.index(old)].count("\n") + 1
with open(os.environ["RP_OUT"], "w", encoding="utf-8") as fh:
    fh.write(src.replace(old, new, 1))
print(f"OK:{line}:{len(old)}:{len(new)}")
PY
)"
case "$APPLY_OUT" in
  OK:*)     IFS=':' read -r _ MUT_LINE LEN_OLD LEN_NEW <<< "$APPLY_OUT" ;;
  ANCHOR:0) die "--old never appears in $FILE_REL. Copy the line exactly as written." ;;
  ANCHOR:*) die "--old appears ${APPLY_OUT#ANCHOR:} times in $FILE_REL; anchor it uniquely
     (include surrounding text) so the mutation cannot half-apply." ;;
  ERROR:*)  die "cannot apply the mutation to $FILE_REL: ${APPLY_OUT#ERROR:}" ;;
  *)        die "cannot apply the mutation to $FILE_REL: $APPLY_OUT" ;;
esac
SHA_MUT="$(sha "$WORK/mutant")"
cat "$WORK/mutant" > "$TARGET" || die "cannot write the mutation to $FILE_REL"
if ! cmp -s "$WORK/mutant" "$TARGET"; then
  die "the mutation did not land in $FILE_REL (short write?); pristine copy: $SNAP"
fi
SAME_LEN=""
[ "$LEN_OLD" = "$LEN_NEW" ] && SAME_LEN="  <-- same byte length as the original: exactly the
                     case that reruns stale bytecode. Handled (-B + __pycache__ purge)."
say "  4. mutated       $FILE_REL:$MUT_LINE${SAME_LEN}"

# --- 5. did anything observable change? (trap 3) --------------------------
if [ -n "$PROBE" ]; then
  purge_bytecode
  ( cd "$REPO" && eval "$PROBE" ) > "$WORK/probe-after.txt" 2>&1
  if cmp -s "$WORK/probe-a.txt" "$WORK/probe-after.txt"; then
    say ""
    say "NOT VERIFIED: the probe produces byte-identical output with and without"
    say "the mutation, so this mutant changes nothing observable. The verdict is"
    say "about the mutation, not about the test. Pick a mutation that bites, or"
    say "widen the probe (round 1's B19 needed rotation AND inline layout before"
    say "it diverged at all; the first sweep found 0/760 differences and would"
    say "have accused the suite falsely)."
    exit 1
  fi
  say "  5. probe         differs from BOTH pristine runs -> the mutant is observable"
fi

# --- 6. the red run -------------------------------------------------------
RC=$(run_pytest red)
IFS='|' read -r V1 N1 E1 <<< "$(classify red)"
say "  6. red run       $V1   ($N1 collected)"
[ -n "$E1" ] && say "                   $E1"

# --- 7. restore and re-prove green ---------------------------------------
# Forgery F5e. The file must still be exactly what we wrote in step 4. If it is
# not, another process has edited it and this run has no right to overwrite it.
OWNER="$(whose_bytes)"
if [ "$OWNER" != "MUTANT" ]; then
  say "  7. restore       *** REFUSED ($OWNER) ***"
  say ""
  say "$FILE_REL is no longer the file this run mutated. Somebody else wrote to"
  say "it between step 4 and now. Restoring would overwrite their work with a"
  say "snapshot taken before it existed, so nothing has been written."
  say ""
  say "  pristine copy            : $SNAP"
  say "  the mutation to undo     : $FILE_REL:$MUT_LINE"
  say "        -   $OLD"
  say "        +   $NEW"
  say ""
  say "Undo the mutation by hand (or merge the pristine copy), then run this"
  say "again on a tree nobody else is editing. This verification is VOID."
  trap - EXIT INT TERM HUP
  exit 2
fi
cat "$SNAP" > "$TARGET"
purge_bytecode
SHA1="$(sha "$TARGET")"
if [ "$SHA0" != "$SHA1" ]; then
  say "  7. restore       *** MISMATCH -- pristine copy is at $SNAP ***"
  trap - EXIT INT TERM HUP
  exit 2
fi
RESTORED=1
say "  7. restore       byte-identical (sha256 ${SHA1:0:16}…)"

RC=$(run_pytest green)
IFS='|' read -r V2 N2 E2 <<< "$(classify green)"
say "  8. green again   $V2   ($N2 collected)"

# --- 8. verdict -----------------------------------------------------------
say ""
FAIL=""
[ "$V1" = "RED_BY_ASSERTION" ] || FAIL="the red run was $V1, not an assertion failure"
[ "$V2" = "GREEN" ] || FAIL="${FAIL:-}${FAIL:+; }the suite is not green again after restore"
[ "$N0" = "$N1" ] && [ "$N1" = "$N2" ] || \
  FAIL="${FAIL:-}${FAIL:+; }collected $N0 / $N1 / $N2 tests -- the mutation broke collection, not behaviour"

if [ -n "$FAIL" ]; then
  say "NOT VERIFIED: $FAIL."
  case "$V1" in
    RED_BY_ERROR|BROKEN)
      say ""
      say "A red for the wrong reason is not a red. $E1"
      say "The mutation crashed the code path before your assertion ran, so this"
      say "test would have passed a board with the defect in it. Make the mutation"
      say "smaller -- a sign, a comparison, a constant, an index."
      show red ;;
    GREEN)
      say ""
      if [ -n "$PROBE" ]; then
        say "The mutant survived, and your probe already ruled out the other"
        say "explanation: it was deterministic on the pristine tree and it moved"
        say "under the mutation. So the mutation is observable and the test does"
        say "not see it. That is a real hole -- write for it."
      else
        say "The mutant survived. Either the test does not cover this line, or the"
        say "mutation is a no-op. Re-run with --probe to tell those two apart before"
        say "you rewrite anything."
      fi ;;
  esac
  exit 1
fi

# --- 9. the trailer, in the run's own words -------------------------------
TRAILER="$(RP_T="$TEST" RP_F="$FILE_REL" RP_L="$MUT_LINE" RP_O="$OLD" RP_N="$NEW" RP_E="$E1" \
  "$PYTHON" - <<'PY'
import os, re
squeeze = lambda s, n: re.sub(r"\s+", " ", s).strip()[:n]
ev = re.sub(r"\s+", " ", os.environ["RP_E"]).strip()
ev = ev[2:].strip() if ev.startswith("E ") else ev
# Keep the `[file:line]` the classifier paired with this message -- trimming it
# off would put the trailer back to quoting a message with no location at all.
m = re.search(r" \[\S+:\d+\]$", ev)
tag = m.group(0) if m else ""
ev = squeeze(ev[: m.start()] if m else ev, max(40, 100 - len(tag))) + tag
print("Red-proof: {t} fails `{e}` when {f}:{l} `{o}` -> `{n}`; green again after restore".format(
    t=os.environ["RP_T"], e=ev, f=os.environ["RP_F"], l=os.environ["RP_L"],
    o=squeeze(os.environ["RP_O"], 60), n=squeeze(os.environ["RP_N"], 60)))
PY
)"

say "VERIFIED. Paste this into the commit message:"
say ""
printf '%s\n' "$TRAILER"
exit 0
