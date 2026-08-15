#!/usr/bin/env python3
"""attribution.py -- does this address actually belong to this person?

The rule that decides whether a name may be attached to an address, in one
place, because three things need it and they must not disagree:

    find_real_emails.clean_name()      before a name is written
    crawler/harvest.py                 (via clean_name)
    pipeline/recheck_attribution.py    after the fact, over stored rows

── Why the test is shaped this way ──

`clean_name()` originally decided whether text near an address was a person by
*excluding* the words that are not names, against a 32-word list. Its docstring
records that the list was added after 131 rows called "Contact Us" reached the
database and became "Hi Contact," in a real cold email.

Twenty more got through anyway, because the list does not contain "sign",
"privacy", "learn", "report" or "acquire":

    Sign In          arjun@example.com
    Privacy Terms    info@example.ai
    Learn More       partnerships@example.com

Filtering by exclusion does not converge. A website produces an unbounded
supply of two-capitalised-word phrases, and every word added to the list only
moves the next false positive one page further along.

So the test is inverted. **A personal mailbox carries some of its owner's
name** -- `tkess@` for Todd Kesselman, `nick@` for Nicholas McCormick. A
navigation label sitting beside `info@` shares nothing with it. The address
becomes the evidence for the name, and unlike a word list, that is bounded.

One thing corroboration cannot do, so `is_label` still exists: a department
mailbox is named after its department, so "Contact Us" corroborates
`contact@example.ai` perfectly. Corroboration proves the address matches the
words. Only vocabulary can say the words are not a person.
"""
import re
import unicodedata

# Zero-width and directional marks. They arrive from copy-pasted directory
# pages, they are invisible in every report, and they survive .strip() -- a
# contact literally named "DY\u200b" is truthy, splits to nothing, and takes
# send_emails.py's greeting line down with an IndexError.
INVISIBLE = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\u200e\u200f\ufeff\u00ad\u2060"), None)

# Short forms that share no prefix with the name they stand for, so no amount
# of substring matching finds them. `mike@example.ai` for Michael Lemm and
# `nick@example.ai` for Nicholas McCormick are both real personal
# addresses this rule condemned before the table existed.
#
# One direction only, short -> long: the address carries the short form and the
# database carries the full name, never the reverse.
NICKNAMES = {
    "mike": "michael", "nick": "nicholas", "bob": "robert", "rob": "robert",
    "bill": "william", "will": "william", "dave": "david", "jim": "james",
    "tom": "thomas", "dan": "daniel", "chris": "christopher", "matt": "matthew",
    "alex": "alexander", "andy": "andrew", "tony": "anthony", "steve": "stephen",
    "joe": "joseph", "rick": "richard", "dick": "richard", "sam": "samuel",
    "ben": "benjamin", "greg": "gregory", "jeff": "jeffrey", "ken": "kenneth",
    "larry": "lawrence", "pete": "peter", "phil": "philip", "ron": "ronald",
    "sue": "susan", "tim": "timothy", "zach": "zachary", "josh": "joshua",
    "nate": "nathan", "gabe": "gabriel", "cam": "cameron", "ted": "edward",
    "kate": "katherine", "kathy": "katherine", "liz": "elizabeth",
    "beth": "elizabeth", "peggy": "margaret", "meg": "margaret",
    "jen": "jennifer", "becky": "rebecca", "trish": "patricia",
}

# Local-parts that are a shared mailbox rather than a person.
#
# These matter for one specific reason: the initials of a two-word name collide
# with them constantly. `hr@` is human resources far more often than it is
# Harish Rana, and `md@` is a shared managing-director mailbox rather than
# Mohan Desai's own. An exact match on one of these is never accepted as
# evidence that an address belongs to a particular person, however senior.
ROLE_LOCALS = {
    "info", "sales", "contact", "contacts", "enquiry", "enquiries", "inquiry",
    "admin", "office", "mail", "email", "support", "help", "helpdesk",
    "marketing", "hr", "careers", "career", "jobs", "recruiting", "recruit",
    "talent", "people", "team", "hello", "hi", "hey", "founders", "founder",
    "press", "media", "partnerships", "partners", "partner", "billing",
    "accounts", "finance", "legal", "privacy", "security", "abuse", "ir",
    "investors", "investor", "webmaster", "postmaster", "noreply", "no-reply",
    "donotreply", "newsletter", "feedback", "service", "services", "customer",
    "customers", "success", "onboarding", "demo", "book", "meet",
    "md", "ceo", "cfo", "cto", "coo", "gm", "vp",
}

# Words that mark a phrase as page furniture rather than a person.
#
# The first block is find_real_emails.NOT_A_PERSON, which this now supersedes;
# the second is what running the rule over the pool actually turned up.
LABEL_WORDS = {
    "contact", "support", "sales", "info", "information", "enquiries", "enquiry",
    "inquiries", "inquiry", "team", "help", "helpdesk", "careers", "jobs",
    "hello", "message", "mail", "email", "touch", "customer", "customers",
    "media", "press", "admin", "office", "service", "services", "billing",
    "partner", "partners", "partnership", "partnerships", "feedback", "demo",
    "us", "we", "here", "now", "today", "more", "learn", "book", "get", "talk",
    "reach", "call", "write", "join", "subscribe", "apply", "request",
} | {
    "sign", "signin", "signup", "login", "log", "register", "privacy", "terms",
    "policy", "cookie", "cookies", "legal", "abuse", "report", "read", "view",
    "click", "home", "about", "menu", "search", "close", "open", "acquire",
    "acquisition", "full", "arrow", "data", "investors", "investor",
    "relations", "relation", "pricing", "docs", "documentation", "blog", "news",
    "download", "start", "started", "free", "trial", "next", "previous",
    "for", "the", "and", "your", "our", "all",
}

SPLIT_NAME = re.compile(r"[^a-z]+")


def deaccent(text: str) -> str:
    """Lowercase, with accented letters reduced to their base letter.

    `jerome@example.ai` spelled `jerome` with two accents is Jerome Scholler's
    own address, so both sides have to reduce to the same ASCII before they can
    be compared.

    The order matters and is the whole subtlety. NFKD splits an accented letter
    into a base letter plus a combining mark; the mark must then be **removed**
    rather than merely treated as a non-letter, because the word-splitter below
    breaks on anything outside a-z. Splitting first turns a six-letter name into
    three fragments, none of which matches anything, and the rule then condemns
    a real address for having an accent in it.
    """
    decomposed = unicodedata.normalize("NFKD", (text or "").translate(INVISIBLE))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def fold(text: str) -> str:
    """Deaccented, with everything that is not a-z dropped."""
    return re.sub(r"[^a-z]", "", deaccent(text))


def local_part(email: str) -> str:
    at = (email or "").rfind("@")
    return email[:at].lower() if at > 0 else ""


def name_words(name) -> list:
    """Significant words in a name, accent-folded to bare ASCII letters."""
    return [w for w in SPLIT_NAME.split(deaccent(name or "")) if w]


def infer_pattern(email, name) -> bool:
    """True when the local-part is one of the shapes a first/last name takes.

    The same set find_real_emails' guess generator emits, read in reverse: here
    a match is evidence *for* the pairing rather than evidence the address was
    invented, because these rows came off a page or a list rather than out of
    that generator.
    """
    parts = name_words(name)
    if len(parts) < 2:
        return False
    first, last = parts[0], parts[-1]
    shapes = {
        first, last, f"{first}.{last}", f"{first}{last}", f"{first[0]}{last}",
        f"{first[0]}.{last}", f"{first}{last[0]}", f"{first}_{last}",
        f"{last}.{first}", f"{last}{first}",
    }
    return local_part(email) in shapes


def is_label(name) -> bool:
    """True when the phrase is page furniture rather than somebody's name.

    Tested per word, never as a substring: "Contact Us" must be rejected while
    "Kilian Justus" is kept, and a substring test on "us" fails that one.
    """
    words = name_words(name)
    if not words:
        return True
    return any(w in LABEL_WORDS for w in words)


def corroborates(name, email) -> bool:
    """Does the address share anything with this person's name?

    Rules in order, weakest evidence last. Three characters is the floor
    throughout: "raj" inside "rajput" is meaningful, "bo" inside "bobby" is a
    coincidence, and a two-letter overlap must never carry an attribution.
    """
    flat = fold(local_part(email or ""))
    if not flat:
        return False
    words = name_words(name)
    if not words:
        return False

    # A whole name word inside the local part: "yarmal" in `s.yarmal`.
    for w in words:
        if len(w) >= 3 and w in flat:
            return True

    # A nickname standing in for the full first name.
    expanded = NICKNAMES.get(flat)
    if expanded and expanded in words:
        return True

    # A shortened first name, either side being the prefix: `guru@` for
    # Gurucharan, `shashi@` for Shashikanth, `nick@` for Nicholas. This clause
    # is absent from the codebase this was ported from, and has to be here:
    # that one reads Indian manufacturer sites where addresses are initials or
    # full surnames, while this pool is full of shortened given names. Without
    # it the rule wrongly condemns 21 real rows.
    for w in words:
        if len(w) >= 4 and len(flat) >= 3 and (w.startswith(flat) or flat.startswith(w)):
            return True

    # first.last, flast, and the other constructed shapes.
    if infer_pattern(email, name):
        return True

    # Initial plus surname: vsingh, akumar, rgupta.
    first, last = words[0], words[-1]
    if len(last) >= 4 and flat == first[0] + last:
        return True

    # Initial plus a *truncated* surname: `tkess@` for Todd Kesselman. Three
    # characters of surname minimum, so a two-letter tail cannot carry it.
    if flat[:1] == first[0] and len(flat) >= 4 and last.startswith(flat[1:]):
        return True

    # The whole name run together, and the contact recorded only as initials.
    squashed = "".join(words)
    if len(squashed) >= 2 and flat == squashed and flat not in ROLE_LOCALS:
        return True

    # Initials -- `lv@` for Louis-Victor, `aj@` for Alok Jain. Leading
    # subsequences too: "F-G Fernandez" answers `f-g@`, the given-name initials
    # with the surname left off. Never a role local-part.
    if flat not in ROLE_LOCALS:
        initials = "".join(w[0] for w in words)
        for n in range(2, len(initials) + 1):
            if flat == initials[:n]:
                return True

    return False
