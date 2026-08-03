// Command verifier is a free-only email verifier: syntax -> MX -> SMTP RCPT
// probe -> catch-all check. It is a port of email_verify.py with no third-party
// dependencies, so a single static binary runs anywhere.
//
// Risk posture (inherited from email_verify.py): when nothing can produce a
// confident answer, the verdict is Unknown, not Invalid. Unknown means "hold
// back, do not send, do not discard" -- it is NOT the same as confirmed-bad.
// Callers must never delete a contact on Unknown.
package main

import (
	"bufio"
	"encoding/csv"
	"errors"
	"flag"
	"fmt"
	"math/rand/v2"
	"net"
	"net/smtp"
	"net/textproto"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"
)

const (
	smtpTimeout    = 6 * time.Second
	interCallDelay = 300 * time.Millisecond
)

// smtpMailFrom and smtpHeloDomain MUST resolve in public DNS.
//
// email_verify.py uses "verify@coldmaildb.local", which does not exist. Any
// mail server that verifies the sender domain answers
//
//	450 4.1.8 Sender address rejected: Domain not found
//
// which this code correctly reads as "inconclusive" -- so a large share of
// perfectly good addresses came back unverifiable for a reason that had
// nothing to do with the address. Defaults now come from GMAIL_ADDRESS.
var (
	smtpMailFrom   = "verify@coldmaildb.local"
	smtpHeloDomain = "coldmaildb.local"
)

// verbose and skipSMTP are written once in main before any goroutine starts,
// then only read.
var (
	verbose  bool
	skipSMTP bool
)

func debugf(format string, args ...any) {
	if verbose {
		fmt.Fprintf(os.Stderr, format, args...)
	}
}

// Verdict is the Go stand-in for Python's `bool | None`.
//
// Unknown is deliberately first so it is the zero value: a Verdict that was
// never assigned reads as "hold back", not "safe to send".
type Verdict int

const (
	Unknown Verdict = iota // not confirmed good, not confirmed bad -- do not send, do not discard
	Valid                  // SMTP accepted it and the domain is not catch-all
	Invalid                // bad syntax, dead domain, or explicit SMTP reject
)

func (v Verdict) String() string {
	switch v {
	case Valid:
		return "valid"
	case Invalid:
		return "invalid"
	default:
		return "unknown"
	}
}

// CatchAll is a separate type from Verdict on purpose. Both have three states,
// but they answer different questions -- reusing Verdict here would let
// "the address is valid" be assigned to "the domain is catch-all" and compile.
type CatchAll int

const (
	CatchAllUnknown CatchAll = iota
	CatchAllYes
	CatchAllNo
)

type Result struct {
	Email     string
	Verdict   Verdict
	CheckedBy string
	Elapsed   time.Duration
}

// Mirrors EMAIL_SYNTAX_RE in email_verify.py.
//
// The domain half allows any number of labels, so foo@netcore.co.in passes.
// An earlier Python version allowed exactly one dot and rejected every .co.in
// address as bad syntax without issuing a single DNS or SMTP query.
//
// \p{L}\p{N} rather than \w, because Go's \w is ASCII-only [0-9A-Za-z_] while
// Python's is Unicode-aware by default. Using \w here rejected 24 live contacts
// in the main database on a first run -- andrés@, benoît@, michał@, nebojša@ and
// similar -- as "invalid syntax", without a single DNS query. Accented local
// parts are ordinary in European contact lists, not an edge case.
var emailSyntax = regexp.MustCompile(`^[\p{L}\p{N}_.+\-]+@[\p{L}\p{N}_\-]+(\.[\p{L}\p{N}_\-]+)*\.[a-zA-Z]{2,}$`)

func validSyntax(email string) bool {
	return emailSyntax.MatchString(email)
}

// domainOf uses LastIndex, not Split: the local part of an address may legally
// contain a quoted "@", and the domain is always after the final one.
func domainOf(email string) string {
	at := strings.LastIndex(email, "@")
	if at < 0 {
		return ""
	}
	return email[at+1:]
}

// lookupMX returns the highest-priority mail server for a domain.
//
// net.LookupMX sorts by preference, so records[0] is the primary -- the Python
// version had to sort manually. It can also return a non-nil error ALONGSIDE
// usable records (malformed names are filtered out and reported); in that case
// the good records are still used, because discarding them would turn one bad
// record into a dead domain.
func lookupMX(domain string) (string, error) {
	records, err := net.LookupMX(domain)
	if len(records) == 0 {
		if err != nil {
			return "", err
		}
		return "", fmt.Errorf("no MX records for %s", domain)
	}
	return strings.TrimSuffix(records[0].Host, "."), nil
}

// checkMX mirrors the MX half of verify_local, including its most important
// detail: a domain that does not exist is a permanent no, but a DNS timeout or
// SERVFAIL is not. Collapsing both into Invalid would permanently discard good
// contacts because a resolver blipped.
func checkMX(domain string) Verdict {
	host, err := lookupMX(domain)
	if err != nil {
		var dnsErr *net.DNSError
		if errors.As(err, &dnsErr) && dnsErr.IsNotFound {
			debugf("  [DNS] %s: no such domain or no MX records\n", domain)
			return Invalid
		}
		debugf("  [DNS] %s: transient failure, holding back: %v\n", domain, err)
		return Unknown
	}
	debugf("  [DNS] %s -> %s\n", domain, host)
	return Valid
}

// dialSMTP exists because smtp.Dial has no timeout parameter -- it will block
// on a tarpitting server until the OS gives up, which can be minutes.
// net.DialTimeout bounds the connect; SetDeadline bounds every read and write
// after it. Python's smtplib.SMTP(timeout=...) covers both in one argument.
func dialSMTP(host string) (*smtp.Client, error) {
	conn, err := net.DialTimeout("tcp", net.JoinHostPort(host, "25"), smtpTimeout)
	if err != nil {
		return nil, err
	}
	if err := conn.SetDeadline(time.Now().Add(smtpTimeout)); err != nil {
		conn.Close()
		return nil, err
	}
	client, err := smtp.NewClient(conn, host)
	if err != nil {
		conn.Close()
		return nil, err
	}
	return client, nil
}

// verifySMTP mirrors verify_smtp: connect to the recipient's mail server and
// issue RCPT TO without ever sending a message body.
func verifySMTP(email string) Verdict {
	domain := domainOf(email)

	host, err := lookupMX(domain)
	if err != nil {
		debugf("  [SMTP] MX lookup failed for %s: %v\n", domain, err)
		return Unknown
	}

	client, err := dialSMTP(host)
	if err != nil {
		debugf("  [SMTP] connection to %s failed: %v\n", host, err)
		return Unknown
	}
	// Mirrors Python's `finally: server.quit()`. QUIT is the polite close --
	// dropping the socket cold gets noticed and counted against the sender.
	defer func() {
		if err := client.Quit(); err != nil {
			client.Close()
		}
	}()

	if err := client.Hello(smtpHeloDomain); err != nil {
		debugf("  [SMTP] HELO to %s rejected: %v\n", host, err)
		return Unknown
	}
	if err := client.Mail(smtpMailFrom); err != nil {
		debugf("  [SMTP] MAIL FROM to %s rejected: %v\n", host, err)
		return Unknown
	}

	err = client.Rcpt(email)
	if err == nil {
		debugf("  [SMTP] %s: 250 accepted by %s\n", email, host)
		return Valid
	}

	var protoErr *textproto.Error
	if !errors.As(err, &protoErr) {
		// Not a server reply at all -- socket died mid-conversation.
		debugf("  [SMTP] %s: no reply from %s: %v\n", email, host, err)
		return Unknown
	}
	switch protoErr.Code {
	case 550, 551, 553, 554:
		v := classifyRejection(protoErr.Msg)
		debugf("  [SMTP] %s: %d -> %s: %s\n", email, protoErr.Code, v, protoErr.Msg)
		return v
	}
	debugf("  [SMTP] %s: inconclusive %d: %s\n", email, protoErr.Code, protoErr.Msg)
	return Unknown
}

// enhancedStatus pulls the RFC 3463 enhanced status code (e.g. "5.7.1") from
// the front of an SMTP reply, if the server sent one.
var enhancedStatus = regexp.MustCompile(`^\s*([245])\.(\d+)\.(\d+)`)

// senderSideRejection matches replies that are about US -- our IP, our sender
// domain, our reputation -- rather than about the recipient's mailbox.
var senderSideRejection = regexp.MustCompile(`(?i)spamhaus|spamcop|barracuda|sorbs|dnsbl|\brbl\b|` +
	`black-?list|block-?list|blocked using|banned|reputation|not authori[sz]ed|access denied|` +
	`client host|helo|sender address rejected|domain not found|service unavailable|` +
	`too many|rate limit|try again|policy reasons`)

// classifyRejection decides whether a permanent 5xx at RCPT TO means "this
// mailbox does not exist" or "this server refuses to talk to you".
//
// This distinction is the difference between correctly discarding a dead
// address and wrongly deleting a live contact because our sending IP is on a
// blocklist. Python's verify_smtp has no such check -- it maps every 550/551/
// 553/554 straight to False, so a single blocklisted IP would mark an entire
// database invalid.
//
// Preference order:
//  1. RFC 3463 enhanced status: 5.1.x is addressing (bad mailbox) -> Invalid.
//     5.7.x is policy/security (about the sender) -> Unknown.
//  2. Failing that, keyword match on known sender-side language -> Unknown.
//  3. Otherwise assume it really is a bad mailbox -> Invalid.
func classifyRejection(msg string) Verdict {
	if m := enhancedStatus.FindStringSubmatch(msg); m != nil {
		switch m[2] {
		case "1": // 5.1.x -- bad destination mailbox / address
			return Invalid
		case "7": // 5.7.x -- policy, security, authorization: about us, not them
			return Unknown
		}
	}
	if senderSideRejection.MatchString(msg) {
		return Unknown
	}
	return Invalid
}

var googleMXSubstrings = []string{"google.com", "googlemail.com"}

// isGoogleHosted: Google's MTA accepts RCPT TO broadly regardless of whether
// the domain owner configured a catch-all. Probing them the same way flags
// every Google-hosted domain as catch-all -- which is most startups -- and
// holds back good addresses for the wrong reason. Known accepted blind spot.
func isGoogleHosted(domain string) bool {
	records, err := net.LookupMX(domain)
	if err != nil && len(records) == 0 {
		return false
	}
	for _, r := range records {
		host := strings.ToLower(r.Host)
		for _, s := range googleMXSubstrings {
			if strings.Contains(host, s) {
				return true
			}
		}
	}
	return false
}

// fakeLocalPart mirrors random.choices(string.digits, k=10).
func fakeLocalPart() string {
	var b strings.Builder
	b.WriteString("zzz-nonexistent-")
	for i := 0; i < 10; i++ {
		b.WriteByte(byte('0' + rand.IntN(10)))
	}
	return b.String()
}

// isCatchAllDomain probes an obviously-fake address to see whether the server
// accepts RCPT TO for anyone. Such domains only reject unknown mailboxes later,
// during real delivery -- so a 250 on the real address proves nothing.
func isCatchAllDomain(domain string) CatchAll {
	if isGoogleHosted(domain) {
		debugf("  [CATCHALL] %s is Google-hosted, skipping probe\n", domain)
		return CatchAllNo
	}
	probe := fakeLocalPart() + "@" + domain
	debugf("  [CATCHALL] probing %s\n", probe)
	switch verifySMTP(probe) {
	case Valid:
		return CatchAllYes // it accepted garbage -- it accepts everything
	case Invalid:
		return CatchAllNo // it properly rejects unknowns
	default:
		return CatchAllUnknown
	}
}

// VerifyEmail mirrors verify_email. Note the shape: exactly one path reaches
// Valid, and many reach Unknown. That asymmetry is the risk posture.
func VerifyEmail(email string) Result {
	start := time.Now()
	done := func(v Verdict, by string) Result {
		return Result{Email: email, Verdict: v, CheckedBy: by, Elapsed: time.Since(start)}
	}

	debugf("[CHECK] %s\n", email)

	if !validSyntax(email) {
		return done(Invalid, "syntax")
	}

	domain := domainOf(email)

	switch checkMX(domain) {
	case Invalid:
		return done(Invalid, "local_mx")
	case Unknown:
		return done(Unknown, "dns_inconclusive")
	}

	// -no-smtp: syntax and MX only. Everything that survives is Unknown, never
	// Valid -- passing an MX check is not evidence the mailbox exists. Use this
	// when the sending IP is blocklisted or port 25 is blocked, where SMTP
	// results would be noise.
	if skipSMTP {
		return done(Unknown, "mx_only")
	}

	switch verifySMTP(email) {
	case Invalid:
		return done(Invalid, "smtp")
	case Unknown:
		return done(Unknown, "smtp_inconclusive")
	}

	// A 250 from a Google-hosted domain is not evidence of anything.
	//
	// Google's MTA accepts RCPT TO at the gateway for addresses that do not
	// exist, then rejects at delivery time with "550-5.1.1 The email account
	// that you tried to reach does not exist". Measured on 2026-08-02: of 15
	// sends, all 8 bounces were Google-hosted domains the probe had called
	// deliverable, and 0 of 5 non-Google domains bounced. Across the verified
	// candidate pool, 181 of 186 "valid" verdicts were Google-hosted -- so the
	// verdict was meaningless for 97% of the addresses it was applied to.
	//
	// Reporting Unknown here is a large downgrade in apparent coverage and an
	// accurate one. Deliverability for these domains can only be learned by
	// sending and reading the bounce.
	if isGoogleHosted(domain) {
		debugf("  [GOOGLE] %s accepts RCPT for unknown users; 250 proves nothing\n", domain)
		return done(Unknown, "google_unverifiable")
	}

	time.Sleep(interCallDelay)

	switch isCatchAllDomain(domain) {
	case CatchAllYes:
		return done(Unknown, "catchall_unverifiable")
	case CatchAllUnknown:
		// DIVERGES from Python: `if catchall:` treats None as falsy, so an
		// inconclusive catch-all probe there silently returns (True, "smtp").
		// That contradicts the module docstring's own stated posture.
		return done(Unknown, "catchall_inconclusive")
	}

	return done(Valid, "smtp")
}

// readLines accepts one address per line, ignoring blanks and # comments.
func readLines(f *os.File) []string {
	var out []string
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		if line := strings.TrimSpace(scanner.Text()); line != "" && !strings.HasPrefix(line, "#") {
			out = append(out, line)
		}
	}
	return out
}

// writeCSV emits email,verdict,checked_by,elapsed_ms so the Python side can
// consume results without parsing the human-readable table.
func writeCSV(path string, results []Result) error {
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()

	w := csv.NewWriter(f)
	defer w.Flush()

	if err := w.Write([]string{"email", "verdict", "checked_by", "elapsed_ms"}); err != nil {
		return err
	}
	for _, r := range results {
		row := []string{r.Email, r.Verdict.String(), r.CheckedBy, fmt.Sprint(r.Elapsed.Milliseconds())}
		if err := w.Write(row); err != nil {
			return err
		}
	}
	w.Flush()
	return w.Error()
}

func main() {
	workers := flag.Int("workers", 4, "concurrent verifications; raising this hits the same MX harder")
	out := flag.String("out", "", "write results to this CSV file as well as stdout")
	from := flag.String("from", "", "MAIL FROM address; must resolve in public DNS (default $GMAIL_ADDRESS)")
	flag.BoolVar(&verbose, "v", false, "log every DNS and SMTP step to stderr")
	flag.BoolVar(&skipSMTP, "no-smtp", false, "syntax and MX only; no SMTP probe (use when the sending IP is blocklisted)")
	flag.Parse()

	if *workers < 1 {
		*workers = 1
	}

	if *from == "" {
		*from = os.Getenv("GMAIL_ADDRESS")
	}
	if *from != "" && validSyntax(*from) {
		smtpMailFrom = *from
		smtpHeloDomain = domainOf(*from)
	} else if !skipSMTP {
		fmt.Fprintf(os.Stderr,
			"[WARN] no valid -from address (and $GMAIL_ADDRESS unset); using %s, which does not\n"+
				"       resolve. Expect widespread 450 sender-rejected and unknown verdicts.\n", smtpMailFrom)
	}

	emails := flag.Args()
	if len(emails) == 0 {
		emails = readLines(os.Stdin)
	}
	if len(emails) == 0 {
		fmt.Fprintln(os.Stderr, "usage: verifier [-v] [-workers N] [-out results.csv] addr... | verifier < list.txt")
		os.Exit(2)
	}

	debugf("[START] %d addresses, %d workers\n", len(emails), *workers)
	start := time.Now()

	jobs := make(chan string)
	results := make(chan Result)

	var wg sync.WaitGroup
	for i := 0; i < *workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for email := range jobs {
				results <- VerifyEmail(email)
			}
		}()
	}

	go func() {
		for _, e := range emails {
			jobs <- e
		}
		close(jobs) // tells every worker's range loop to end
	}()

	go func() {
		wg.Wait()      // all workers done...
		close(results) // ...so nothing else will be sent
	}()

	tally := map[Verdict]int{}
	collected := make([]Result, 0, len(emails))
	for r := range results {
		tally[r.Verdict]++
		collected = append(collected, r)
		fmt.Printf("%-45s %-8s %-24s %6dms\n", r.Email, r.Verdict, r.CheckedBy, r.Elapsed.Milliseconds())
	}

	if *out != "" {
		if err := writeCSV(*out, collected); err != nil {
			fmt.Fprintf(os.Stderr, "[ERROR] writing %s: %v\n", *out, err)
			os.Exit(1)
		}
		fmt.Fprintf(os.Stderr, "[WROTE] %s (%d rows)\n", *out, len(collected))
	}

	fmt.Fprintf(os.Stderr, "\n[DONE] %d checked in %s -- valid=%d invalid=%d unknown=%d\n",
		len(emails), time.Since(start).Round(time.Millisecond),
		tally[Valid], tally[Invalid], tally[Unknown])
}
