"""Match a resume against a job description.

The split of labour matters here. This module does the part that should be
deterministic and reproducible:

* pull candidate requirement terms out of the JD, weighted by where they
  appear (a term under "Requirements" outranks one in the boilerplate);
* decide whether the resume contains each term, expanding known aliases so
  "K8s" satisfies "Kubernetes" and "JS" satisfies "JavaScript";
* notice terms the resume only lists in a skills blob without any supporting
  bullet -- an assertion no interviewer will find evidence for;
* extract hard gates (years of experience, degree, language, clearance).

It deliberately does *not* try to judge whether "led a migration" satisfies
"experience with large-scale system design". That is a semantic call, and the
model reading this output is far better at it than a regex. The rule is:
this module reports evidence, the model reports the verdict.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

__all__ = ["TermHit", "MatchReport", "match", "load_aliases"]

_HERE = os.path.dirname(os.path.abspath(__file__))
_ALIAS_PATH = os.path.join(_HERE, "..", "references", "aliases.json")

STOPWORDS: Set[str] = {
    "a", "about", "above", "across", "after", "all", "also", "an", "and", "any", "are", "as", "at",
    "be", "been", "being", "both", "but", "by", "can", "could", "do", "does", "doing", "each",
    "etc", "for", "from", "further", "had", "has", "have", "having", "he", "her", "here", "his",
    "how", "if", "in", "into", "is", "it", "its", "just", "may", "might", "more", "most", "must",
    "no", "nor", "not", "of", "off", "on", "once", "only", "or", "other", "our", "out", "over",
    "own", "per", "same", "she", "should", "so", "some", "such", "than", "that", "the", "their",
    "them", "then", "there", "these", "they", "this", "those", "through", "to", "too", "under",
    "up", "us", "use", "using", "very", "was", "we", "were", "what", "when", "where", "which",
    "while", "who", "why", "will", "with", "within", "would", "you", "your", "yours",
    # JD boilerplate that is never a requirement
    "ability", "based", "candidate", "candidates", "company", "employer", "equal", "excellent",
    "experience", "gender", "good", "great", "help", "including", "join", "job", "life", "look",
    "looking", "love", "new", "offer", "office", "opportunity", "orientation", "part", "people",
    "please", "position", "race", "regard", "religion", "role", "salary", "strong", "team",
    "teams", "us", "want", "work", "working", "years", "year", "plus", "benefits", "culture",
    "environment", "fast", "paced", "hybrid", "remote", "onsite", "apply", "applicants",
    "responsibilities", "requirements", "qualifications", "preferred", "required", "nice",
    "bonus", "day", "days", "week", "month", "months", "time", "full", "well", "make", "made",
    "get", "got", "like", "need", "needs", "you'll", "we're", "you're", "our", "including",
    # Qualifier words that open a requirement bullet. They are capitalised by
    # position, not because they name anything, so without this list they get
    # harvested as if they were proper nouns.
    "deep", "solid", "hands", "hands-on", "handson", "proven", "demonstrable",
    "demonstrated", "exposure", "familiarity", "familiar", "knowledge", "understanding",
    "proficiency", "proficient", "expertise", "expert", "advanced", "basic", "extensive",
    "significant", "track", "record", "comfortable", "passionate", "curious", "self",
    "starter", "detail", "oriented", "minded", "willing", "eager", "able", "capable",
    "highly", "deeply", "ideally", "preferably", "essential", "desirable", "mandatory",
    "responsible", "ensure", "ensuring", "build", "building", "built", "develop",
    "developing", "design", "designing", "maintain", "maintaining", "support",
    "supporting", "deliver", "delivering", "drive", "driving", "own", "owning",
    "collaborate", "collaborating", "partner", "partnering", "contribute",
}

# Words that never stand alone as a requirement even though they look technical.
WEAK_UNIGRAMS: Set[str] = {
    "api", "apis", "protocol", "protocols", "buffer", "buffers", "service", "services",
    "system", "systems", "tool", "tools", "platform", "platforms", "framework",
    "frameworks", "library", "libraries", "language", "languages", "database",
    "databases", "cloud", "code", "software", "application", "applications",
    "engineer", "engineering", "developer", "development", "senior", "junior", "lead",
    "staff", "principal", "manager", "science", "computer", "technology", "technologies",
}

# Sections whose bullets carry the real gating criteria.
REQ_SECTION_RE = re.compile(
    r"^\s*(?:what\s+you'?ll\s+need|what\s+we'?re\s+looking\s+for|requirements?|qualifications?|"
    r"must[- ]haves?|you\s+have|about\s+you|skills?\s*(?:&|and)?\s*experience|who\s+you\s+are|"
    r"minimum\s+qualifications?|basic\s+qualifications?|requisiti|competenze\s+richieste)\b",
    re.I | re.M,
)
NICE_SECTION_RE = re.compile(
    r"^\s*(?:nice[- ]to[- ]haves?|bonus(?:\s+points?)?|preferred(?:\s+qualifications?)?|"
    r"desirable|plus(?:es)?|would\s+be\s+(?:a\s+)?(?:plus|great)|gradito)\b",
    re.I | re.M,
)
BENEFIT_SECTION_RE = re.compile(
    r"^\s*(?:benefits?|what\s+we\s+offer|perks?|why\s+join|compensation|about\s+(?:us|the\s+company)|"
    r"our\s+(?:values|mission)|equal\s+opportunity|diversity|cosa\s+offriamo)\b",
    re.I | re.M,
)

YEARS_RE = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:-|to|–)?\s*(\d{1,2})?\s*\+?\s*(?:years?|yrs?|anni)\b"
    r"(?:\s+(?:of|in|with|as|di))?\s*([^.;,\n]{0,60})",
    re.I,
)
DEGREE_RE = re.compile(
    r"\b(bachelor'?s?|master'?s?|phd|doctorate|bsc|msc|b\.?s\.?|m\.?s\.?|mba|laurea|degree)\b"
    r"(?:\s+(?:degree\s+)?(?:in|di)\s+([A-Za-z ,/&]{3,50}))?",
    re.I,
)
LANGUAGE_RE = re.compile(
    r"\b(fluent|native|proficient|business[- ]level|c1|c2|b2)\b[^.\n]{0,30}?\b"
    r"(english|italian|german|french|spanish|portuguese|dutch|inglese|italiano|tedesco)\b",
    re.I,
)


def load_aliases(path: Optional[str] = None) -> Dict[str, List[str]]:
    """canonical term -> list of surface forms that also count as a match."""
    path = path or _ALIAS_PATH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    out: Dict[str, List[str]] = {}
    for canon, forms in raw.items():
        if isinstance(forms, list):
            out[canon.lower()] = [str(f).lower() for f in forms]
    return out


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("’", "'").replace("‘", "'")
    text = re.sub(r"[‐-―]", "-", text)
    return text


def _tokenise(text: str) -> List[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9+#.\-]*[A-Za-z0-9+#]|[A-Za-z]", text)


def _singular(word: str) -> str:
    low = word.lower()
    for suffix, repl in (("ies", "y"), ("sses", "ss"), ("ses", "s"), ("s", "")):
        if low.endswith(suffix) and len(low) - len(suffix) >= 3:
            return low[: len(low) - len(suffix)] + repl
    return low


def _canonical(term: str) -> str:
    return " ".join(_singular(t) for t in term.lower().split())


@dataclass
class TermHit:
    term: str
    canonical: str
    weight: float
    section: str  # required | preferred | body
    jd_count: int
    in_resume: bool
    matched_via: str = ""
    resume_count: int = 0
    evidence: str = ""
    skills_list_only: bool = False


@dataclass
class Gate:
    kind: str  # years | degree | language
    text: str
    value: Optional[float] = None
    subject: str = ""
    satisfied: Optional[bool] = None
    note: str = ""


@dataclass
class MatchReport:
    coverage_required: float
    coverage_all: float
    matched: List[TermHit] = field(default_factory=list)
    missing: List[TermHit] = field(default_factory=list)
    skills_list_only: List[TermHit] = field(default_factory=list)
    gates: List[Gate] = field(default_factory=list)
    jd_sections_found: List[str] = field(default_factory=list)
    resume_years_estimate: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "coverage_required": round(self.coverage_required, 3),
            "coverage_all": round(self.coverage_all, 3),
            "matched": [asdict(t) for t in self.matched],
            "missing": [asdict(t) for t in self.missing],
            "skills_list_only": [asdict(t) for t in self.skills_list_only],
            "gates": [asdict(g) for g in self.gates],
            "jd_sections_found": self.jd_sections_found,
            "resume_years_estimate": self.resume_years_estimate,
        }


def _split_jd_sections(jd: str) -> List[Tuple[str, str]]:
    """Label each block of the JD as required / preferred / benefits / body."""
    lines = jd.split("\n")
    blocks: List[Tuple[str, List[str]]] = []
    current = "body"
    buf: List[str] = []
    for line in lines:
        if REQ_SECTION_RE.match(line):
            blocks.append((current, buf))
            current, buf = "required", []
            continue
        if NICE_SECTION_RE.match(line):
            blocks.append((current, buf))
            current, buf = "preferred", []
            continue
        if BENEFIT_SECTION_RE.match(line):
            blocks.append((current, buf))
            current, buf = "benefits", []
            continue
        buf.append(line)
    blocks.append((current, buf))
    return [(label, "\n".join(body)) for label, body in blocks if "".join(body).strip()]


def _candidate_terms(text: str, lexicon: Set[str]) -> Dict[str, Tuple[int, str]]:
    """n-gram candidates (1-3 words) -> (count, representative surface form).

    The surface form is kept for display: the canonical key is singularised,
    so reporting it directly would tell the user their resume is missing
    "kubernete".
    """
    counts: Dict[str, int] = {}
    display: Dict[str, str] = {}
    for raw_line in text.split("\n"):
        tokens = _tokenise(_normalise(raw_line))
        lowered = [t.lower() for t in tokens]
        n = len(tokens)
        for i in range(n):
            for size in (1, 2, 3):
                if i + size > n:
                    break
                gram_tokens = lowered[i : i + size]
                if gram_tokens[0] in STOPWORDS or gram_tokens[-1] in STOPWORDS:
                    continue
                if any(len(t) < 2 for t in gram_tokens):
                    continue
                gram = " ".join(gram_tokens)
                canon = _canonical(gram)
                known = canon in lexicon
                if size == 1:
                    tok = gram_tokens[0]
                    if not known:
                        if tok in STOPWORDS or tok in WEAK_UNIGRAMS or len(tok) < 3:
                            continue
                        if len(tok) > 20:
                            continue
                        has_sig = bool(re.search(r"[+#.]|\d", tok))
                        acronym = tokens[i].isupper() and 2 <= len(tok) <= 6
                        # Capitalisation only counts mid-line. A bullet or
                        # sentence opener is capitalised by position, which
                        # is how "Strong", "Deep" and "Proven" sneak in.
                        capitalised = tokens[i][:1].isupper() and i > 0
                        if not (has_sig or acronym or capitalised):
                            continue
                elif not known:
                    # Multi-word phrases: require every word capitalised
                    # (mid-line) or a technical marker, else it is prose.
                    caps = sum(1 for t in tokens[i : i + size] if t[:1].isupper())
                    if i == 0:
                        caps -= 1  # discount the line-initial word
                    if caps < size and not re.search(r"[+#.]|\d", gram):
                        continue
                counts[canon] = counts.get(canon, 0) + 1
                display.setdefault(canon, gram)
    return {canon: (n_, display.get(canon, canon)) for canon, n_ in counts.items()}


def _drop_redundant(hits: List[TermHit], lexicon: Set[str]) -> List[TermHit]:
    """Remove terms wholly contained in a longer term that was also selected.

    Without this, one JD line yields "computer", "science" and "computer
    science" as three separate gaps, which triples the apparent problem.
    Lexicon entries are always kept -- "go" inside "golang" is still a real
    skill in its own right.
    """
    kept: List[TermHit] = []
    phrases = [(h, set(h.canonical.split())) for h in hits]
    for hit, words in phrases:
        if hit.canonical in lexicon:
            kept.append(hit)
            continue
        redundant = False
        for other, other_words in phrases:
            if other is hit:
                continue
            if len(other_words) <= len(words):
                continue
            # Subsumed by a longer phrase that is at least as prominent.
            if words < other_words and other.weight >= hit.weight * 0.6:
                redundant = True
                break
        if not redundant:
            kept.append(hit)
    return kept


def _build_lexicon(
    aliases: Dict[str, List[str]],
) -> Tuple[Set[str], Dict[str, str], Dict[str, str]]:
    """Return (canonical set, surface->canonical index, canonical->spelling).

    The third map exists because canonical keys are singularised for matching:
    without it the report tells the user they are missing "kubernete".
    """
    lexicon: Set[str] = set()
    index: Dict[str, str] = {}
    spelling: Dict[str, str] = {}
    for canon, forms in aliases.items():
        c = _canonical(canon)
        lexicon.add(c)
        index[c] = c
        spelling[c] = canon
        for form in forms:
            index[_canonical(form)] = c
    return lexicon, index, spelling


def _resume_index(resume: str) -> Tuple[str, Set[str], str]:
    """Return (normalised lowercase text, token set, skills-section text)."""
    norm = _normalise(resume)
    low = norm.lower()
    tokens = {_singular(t) for t in _tokenise(low)}

    skills_text = ""
    # Each repetition consumes exactly one whole line. Writing this as
    # `(?:.+\n?){0,12}?` instead lets `.+` and the outer quantifier split the
    # same line many ways, which backtracks exponentially on real resumes.
    m = re.search(
        r"^\s*(?:technical\s+)?(?:skills?|competenc(?:e|ies)|competenze|tech\s+stack|technologies)\b[^\n]*\n"
        r"((?:[^\n]*\n){0,12}?)(?:[ \t]*\n|\Z)",
        norm,
        re.I | re.M,
    )
    if m:
        skills_text = m.group(1).lower()
    return low, tokens, skills_text


def _find_in_resume(
    canon: str,
    aliases: Dict[str, List[str]],
    resume_low: str,
    resume_tokens: Set[str],
) -> Tuple[bool, str, int, str]:
    """Does the resume contain this term? Returns (found, via, count, evidence)."""
    forms = [canon] + [_canonical(f) for f in aliases.get(canon, [])]
    for form in dict.fromkeys(forms):
        if not form:
            continue
        if " " in form:
            pattern = r"\b" + r"\W+".join(re.escape(w) for w in form.split()) + r"\w*"
        else:
            # Allow a plural/inflected tail but not an unrelated longer word.
            pattern = r"\b" + re.escape(form) + r"(?:s|es|ing|ed)?\b"
        hits = re.findall(pattern, resume_low, re.I)
        if hits:
            idx = re.search(pattern, resume_low, re.I)
            start = max(0, idx.start() - 60)
            evidence = resume_low[start : idx.end() + 60].replace("\n", " ").strip()
            via = "exact" if form == canon else f"alias:{form}"
            return True, via, len(hits), evidence
        if " " not in form and form in resume_tokens:
            return True, "token", 1, ""
    return False, "", 0, ""


def _estimate_resume_years(resume: str) -> Optional[float]:
    """Rough total tenure from date ranges, merging overlaps."""
    spans: List[Tuple[int, int]] = []
    now_year = 2026
    for m in re.finditer(
        r"\b((?:19|20)\d{2})\s*[-–—]\s*((?:19|20)\d{2}|present|current|now|ongoing|oggi|attuale)\b",
        resume,
        re.I,
    ):
        start = int(m.group(1))
        end_raw = m.group(2).lower()
        end = now_year if not end_raw.isdigit() else int(end_raw)
        if 1950 < start <= end <= now_year + 1:
            spans.append((start, end))
    if not spans:
        return None
    spans.sort()
    merged: List[List[int]] = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return float(sum(e - s for s, e in merged))


def _extract_gates(jd: str, resume: str, resume_low: str) -> List[Gate]:
    gates: List[Gate] = []
    seen: Set[str] = set()

    resume_years = _estimate_resume_years(resume)
    for m in YEARS_RE.finditer(jd):
        lo = int(m.group(1))
        subject = (m.group(3) or "").strip(" .,;:-")
        key = f"years:{lo}:{subject[:20]}"
        if key in seen:
            continue
        seen.add(key)
        gate = Gate(kind="years", text=m.group(0).strip()[:90], value=float(lo), subject=subject)
        if resume_years is not None:
            gate.satisfied = resume_years >= lo
            gate.note = (f"date ranges in the resume span about {resume_years:.0f} years "
                         f"in total; this counts every dated range, education included")
        else:
            gate.note = "could not estimate total years from the resume's dates"
        gates.append(gate)
        if len(gates) >= 6:
            break

    for m in DEGREE_RE.finditer(jd):
        level = m.group(1).lower()
        key = f"deg:{level}"
        if key in seen:
            continue
        seen.add(key)
        field_of = (m.group(2) or "").strip(" .,;:-")
        gate = Gate(kind="degree", text=m.group(0).strip()[:90], subject=field_of)
        gate.satisfied = bool(
            re.search(r"\b(bachelor|master|phd|doctorate|bsc|msc|b\.?s\.?|m\.?s\.?|mba|laurea|degree)\b",
                      resume_low, re.I)
        )
        if not gate.satisfied:
            gate.note = "no degree keyword found in the resume"
        gates.append(gate)
        if sum(1 for g in gates if g.kind == "degree") >= 2:
            break

    for m in LANGUAGE_RE.finditer(jd):
        lang = m.group(2).lower()
        key = f"lang:{lang}"
        if key in seen:
            continue
        seen.add(key)
        gate = Gate(kind="language", text=m.group(0).strip()[:90], subject=lang)
        gate.satisfied = lang in resume_low
        if not gate.satisfied:
            gate.note = f"'{lang}' is not mentioned in the resume"
        gates.append(gate)

    return gates


def match(
    resume_text: str,
    jd_text: str,
    aliases: Optional[Dict[str, List[str]]] = None,
    max_terms: int = 60,
) -> MatchReport:
    aliases = aliases if aliases is not None else load_aliases()
    lexicon, surface_index, spelling = _build_lexicon(aliases)
    # Terms are looked up by their canonical (singularised) form, so the alias
    # table has to be keyed that way too. Keying it on the raw JSON key means
    # "kubernetes" never resolves for a term canonicalised to "kubernete", and
    # every K8s-only resume gets told it is missing Kubernetes.
    aliases = {_canonical(k): v for k, v in aliases.items()}
    resume_low, resume_tokens, skills_text = _resume_index(resume_text)

    sections = _split_jd_sections(jd_text)
    weights = {"required": 3.0, "preferred": 1.5, "body": 1.0, "benefits": 0.0}

    scored: Dict[str, TermHit] = {}
    for label, body in sections:
        w = weights.get(label, 1.0)
        if w == 0.0:
            continue
        for raw_canon, (count, surface) in _candidate_terms(body, lexicon).items():
            # Fold surface variants onto their canonical term.
            canon = surface_index.get(raw_canon, raw_canon)
            # Prefer the lexicon's own spelling, else the form seen in the JD.
            display = spelling.get(canon, surface) if canon in lexicon else surface
            weight = w * (1 + 0.25 * (count - 1))
            existing = scored.get(canon)
            if existing is None:
                scored[canon] = TermHit(
                    term=display, canonical=canon, weight=weight,
                    section=label, jd_count=count, in_resume=False,
                )
            else:
                existing.weight += weight
                existing.jd_count += count
                if weights.get(label, 1.0) > weights.get(existing.section, 1.0):
                    existing.section = label

    # Known skills outrank incidental capitalised words.
    for canon, hit in scored.items():
        if canon in lexicon:
            hit.weight *= 1.6

    ranked = sorted(scored.values(), key=lambda t: (-t.weight, t.term))[:max_terms]
    ranked = _drop_redundant(ranked, lexicon)

    for hit in ranked:
        found, via, count, evidence = _find_in_resume(hit.canonical, aliases, resume_low, resume_tokens)
        hit.in_resume = found
        hit.matched_via = via
        hit.resume_count = count
        hit.evidence = evidence[:180]
        if found and skills_text:
            in_skills = hit.canonical in skills_text or any(
                _canonical(f) in skills_text for f in aliases.get(hit.canonical, [])
            )
            # Present in the skills blob and nowhere else = an unsupported claim.
            hit.skills_list_only = bool(in_skills and count <= 1)

    matched = [h for h in ranked if h.in_resume]
    missing = [h for h in ranked if not h.in_resume]
    skills_only = [h for h in matched if h.skills_list_only]

    def coverage(items: Sequence[TermHit]) -> float:
        total = sum(h.weight for h in items)
        got = sum(h.weight for h in items if h.in_resume)
        return got / total if total else 0.0

    required = [h for h in ranked if h.section == "required"]
    cov_req = coverage(required) if required else coverage(ranked)

    return MatchReport(
        coverage_required=cov_req,
        coverage_all=coverage(ranked),
        matched=matched,
        missing=missing,
        skills_list_only=skills_only,
        gates=_extract_gates(jd_text, resume_text, resume_low),
        jd_sections_found=[label for label, _ in sections],
        resume_years_estimate=_estimate_resume_years(resume_text),
    )
