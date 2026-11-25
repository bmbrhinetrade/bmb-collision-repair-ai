"""Red-flag detection: negative company events, denials, hijacked-account scams and rumor markers.

Pure, deterministic regex families (no I/O), used by:

* the rules-only fallback score (``scoring/claude.py``): ``red_flag`` / ``red_flag_types`` when Claude is down;
* the Claude prompt (``scoring/prompt.py``): fixed labels only, never post text, as hints;
* credibility (``scoring/credibility.py``): :func:`detect_compromise` on watched accounts;
* the pipeline: :func:`detect_denial` for "denied" alerts.

Direction matters. Every family matches the NEGATIVE event only: "cuts guidance" is a red flag, "raises
guidance" is not; a complete response letter / clinical hold / missed primary endpoint is a clinical
setback, an FDA approval is not; "fraud detection" / "we blocked $40B in fraud" marketing (Visa,
Mastercard) is not fraud. A match framed as a question, condition or negation ("if the FDA issues a
CRL?", "Tesla is NOT under investigation") is reported as *hedged*: a hint for Claude, never a rules-only
red flag. A match right after a positive legal outcome ("wins the lawsuit", "SEC closes its
investigation", "clinical hold lifted") does not count at all.

Hardening (same rules as scoring/rules.py, SEC-1): the scanned text is capped at
:data:`MAX_TEXT_CHARS`, every gap is bounded, no quantifier nests over overlapping classes, match starts
are anchored with look-behinds so a crafted 25,000-character post scans in linear time, and at most
:data:`MAX_MATCHES_PER_PATTERN` matches per pattern are examined.
"""

from __future__ import annotations

import functools
import itertools
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from re import Pattern
from urllib.parse import urlsplit

from ..models import RED_FLAG_TYPES

#: Only this much text is scanned (X posts can be 25,000 characters; Claude still sees all of it).
MAX_TEXT_CHARS = 8_000
#: At most this many matches per pattern are checked for hedges / exclusions (bounded work per post).
MAX_MATCHES_PER_PATTERN = 8

T_SHORT = "short_report"
T_FRAUD = "fraud_accounting"
T_LEGAL = "legal_regulatory"
T_SAFETY = "product_safety"
T_FINANCIAL = "financial_warning"
T_EXEC = "executive"
T_DOWNGRADE = "analyst_downgrade"
T_SECURITY = "security_incident"
T_COMPROMISE = "account_compromise"
T_CLINICAL = "clinical_setback"
T_OTHER = "other"

#: Human-readable names for alerts and the Claude hints.
TYPE_LABELS: dict[str, str] = {
    T_SHORT: "short report",
    T_FRAUD: "fraud / accounting",
    T_LEGAL: "legal / regulatory",
    T_SAFETY: "product safety",
    T_FINANCIAL: "financial warning",
    T_EXEC: "executive",
    T_DOWNGRADE: "analyst downgrade",
    T_SECURITY: "security incident",
    T_COMPROMISE: "account compromise",
    T_CLINICAL: "clinical setback",
    T_OTHER: "other",
}
assert set(TYPE_LABELS) == set(RED_FLAG_TYPES)

_LB = r"(?<![A-Za-z0-9_])"
_RB = r"(?![A-Za-z0-9_])"
# a bounded "a few words" gap: each word is a run of word characters, so the gap cannot overlap the spaces
_GAP2 = r"(?:[\w$.&'’-]+\s+){0,2}?"
_GAP3 = r"(?:[\w$.&'’-]+\s+){0,3}?"
_GAP4 = r"(?:[\w$.&'’-]+\s+){0,4}?"


def _w(*alternatives: str) -> str:
    """Alternatives between word boundaries; the leading look-ahead makes non-letter positions fail at once."""
    return r"(?=[A-Za-z$0-9])" + _LB + "(?:" + "|".join(alternatives) + ")" + _RB


#: family contexts with extra checks (see _scan_red_flags)
CTX_SECURITY = "security"  # breach / hack: skipped in protective or threat-research context ("stop cyberattacks")
CTX_OUTAGE = "outage"  # outages: skipped in protective / marketing context ("zero outages") and planned maintenance
CTX_HALT = "halt"  # trading halts: skipped for scheduled halts ("halt trading ... for the Thanksgiving holiday")
CTX_PLAINTIFF = "plaintiff"  # "X filed a lawsuit against Y": bad news for Y; a company posting it is the plaintiff


@dataclass(frozen=True)
class _Family:
    type: str
    label: str  # fixed vocabulary shown in hints and alerts, never post text
    rx: Pattern[str]
    ctx: str = ""


def _fam(type_: str, label: str, pattern: str, *, cs: bool = False, ctx: str = "") -> _Family:
    return _Family(type_, label, re.compile(pattern, 0 if cs else re.IGNORECASE), ctx)


# --------------------------------------------------------------------------- building blocks

# ("president" is left out on purpose: "President departs for Asia" is politics, not a company exit)
_ROLE = r"(?:CEO|CFO|COO|CTO|chief\s+[a-z]+\s+officer|chair(?:man|woman|person)?|co-?founder|founder|general\s+counsel)"
_REG_CS = (
    r"(?:SEC|DOJ|FTC|CFTC|FBI|FINRA|NHTSA|FAA|FDA|FCC|CFPB|OCC|IRS|EPA|OSHA|ITC|CMA|ACCC|SAMR|EU"
    r"|(?i:European\s+Commission|state\s+attorneys?\s+general|attorneys?\s+general|regulators?|prosecutors?"
    r"|Justice\s+Department|Department\s+of\s+Justice|federal\s+investigators|investigators|authorities"
    r"|lawmakers|the\s+feds))"
)
_ENDPOINT = (
    r"(?:(?:co-?)?primary|key\s+secondary|secondary)\s+(?:and\s+(?:key\s+)?secondary\s+)?(?:efficacy\s+)?end-?points?"
)
_TRIAL = r"(?<!free\s)(?<!free-)(?:trials?|stud(?:y|ies))"
_GUIDANCE_OBJ = (
    r"(?:(?:its|the|our|their|full[-\s]year|annual|FY\s?['’]?\d{2,4}|20\d{2}|quarterly|Q[1-4]|revenue|sales"
    r"|profit|earnings|delivery|deliveries|margin|production|EPS)\s+){0,3}(?:guidance|outlook|forecasts?"
    r"|targets?|estimates)"
)

# --------------------------------------------------------------------------- red-flag families

_FAMILIES: tuple[_Family, ...] = (
    # ---- short reports
    _fam(T_SHORT, "short report", _w(r"short[-\s]?(?:sell(?:er|ers|ing)['’]?s?\s+)?reports?")),
    _fam(
        T_SHORT,
        "short seller",
        _w(
            r"short[-\s]sellers?\s+" + _GAP3 + r"(?:alleg\w*|accus\w*|claims?|claimed|says|said|targets?|targeted"
            r"|publish\w*|releas\w*|issu\w*|report\w*)"
        ),
    ),
    _fam(
        T_SHORT,
        "public short position",
        # "We are short $SMCI", "we're short Supermicro." (not "short on time", "short of cash")
        r"(?<![A-Za-z0-9_])(?i:we\s+are|we['’]re|I\s+am|I['’]m|we\s+remain|we\s+have\s+been)\s+"
        r"(?i:now\s+|also\s+|still\s+)?(?i:short)(?=\s+\$?[A-Z]|\s*[.!]|\s*$)",
        cs=True,
    ),
    _fam(
        T_SHORT,
        "short position",
        _w(
            r"(?:short|bearish)\s+(?:position|bet|thesis|case)\s+(?:on|in|against)",
            r"(?:open\w*|initiat\w*|disclos\w*|tak(?:e|es|ing|en)|took|establish\w*|add\w*|new)\s+"
            r"(?:an?\s+|our\s+)?(?:new\s+)?short\s+(?:position|bet|thesis)",
            r"short\s+thesis",
        ),
    ),
    _fam(
        T_SHORT,
        "short-seller firm",
        _LB
        + r"(?:Hindenburg|Muddy\s+Waters|Citron|Gotham\s+City|Spruce\s+Point|Wolfpack|Kerrisdale|Grizzly"
        r"|Culper|Fuzzy\s+Panda|Blue\s+Orca|Viceroy|Scorpion\s+Capital|Iceberg\s+Research|Bleecker\s+Street"
        r"|J\s+Capital|White\s+Diamond|Morpheus|Bonitas|Glaucus|Jehoshaphat|Capybara)"
        r"(?:\s+(?:Research|Capital|Analytics))?\s+(?i:(?:is\s+|has\s+|just\s+)?(?:short|shorting|report\w*"
        r"|alleg\w*|publish\w*|releas\w*|target\w*|accus\w*|out\s+with))" + _RB,
        cs=True,
    ),
    # ---- fraud / accounting (strong phrases; a bare "fraud" is checked separately for marketing context)
    _fam(
        T_FRAUD,
        "fraud allegation",
        _w(
            r"(?:securities|accounting|wire|bank|investor|financial|massive|outright|corporate|alleged)\s+fraud",
            r"fraud\s+(?:allegations?|charges?|claims?|scheme|scandal|probe|investigation|lawsuit|case|trial"
            r"|conviction)",
            r"(?:alleg\w+|accus\w+|charg\w+|convict\w+|guilty)\s+" + _GAP4 + r"(?:of\s+|with\s+)?(?:\w+\s+)?fraud",
            r"(?:committ\w+|perpetrat\w+|engag\w+\s+in)\s+(?:a\s+)?(?:\w+\s+)?fraud",
            r"(?:is|was|are|were)\s+(?:a\s+)?(?:total\s+|complete\s+|massive\s+|giant\s+)?fraud"
            r"(?![-\s]+(?:prevention|detection|protection|proof|resistant|free))",
            r"ponzi",
            r"embezzl\w+",
            r"misappropriat\w+",
        ),
    ),
    _fam(
        T_FRAUD,
        "accounting irregularities",
        _w(
            r"accounting\s+(?:irregularit\w+|issues|problems|scandal|manipulation|errors|concerns|questions"
            r"|improprieties|misconduct)",
            r"cook(?:ed|ing|s)?\s+the\s+books",
            r"(?:inflat\w+|overstat\w+|fabricat\w+|fak(?:ed|ing)|manipulat\w+|falsif\w+)\s+(?:(?:its|their|the|our)\s+)?"
            r"(?:revenue|revenues|sales|earnings|numbers|financials|users|customers|results|books|accounts|profits?"
            r"|deliveries|bookings)",
            r"channel[-\s]stuffing",
            r"round[-\s]tripp?ing",
            r"related[-\s]party\s+(?:transactions|sales|dealings|revenue)",
            r"earnings\s+manipulation",
        ),
    ),
    _fam(
        T_FRAUD,
        "restatement",
        _w(
            r"(?:financial|earnings|accounting)\s+restatements?",
            r"restatements?(?!\s+of\s+(?:our|its|the|their)\s+(?:mission|values|vision|goals|commitment|purpose))",
            r"restat(?:e|es|ed|ing|ement|ements)\s+(?:of\s+)?(?:(?:its|their|our|the|prior|previous|past|several|three"
            r"|two)\s+){0,3}" + _GAP2 + r"(?:financial|financials|earnings|results|statements|revenue|quarters|years?)",
        ),
    ),
    _fam(T_FRAUD, "going concern", _w(r"going[-\s]concern")),
    _fam(T_FRAUD, "material weakness", _w(r"material\s+weakness(?:es)?")),
    _fam(
        T_FRAUD,
        "auditor exit",
        _w(
            r"auditors?\s+" + _GAP2 + r"(?:resign\w*|quit|quits|steps?\s+down|stepped\s+down|withdr\w+|exit\w*"
            r"|depart\w*|fired|dismiss\w*|walk\w*\s+away)",
            r"(?:resign\w*|resignation|exit|departure|dismissal|withdrawal)\s+of\s+(?:(?:its|the|our|their)\s+)?"
            + _GAP2 + r"auditors?",
            # "Ernst & Young resigns as Super Micro's auditor" (the Oct 2024 headline)
            r"(?:resign(?:s|ed|ing)?|quit(?:s)?|steps?\s+down|stepped\s+down|withdr\w+)\s+as\s+" + _GAP3
            + r"(?:independent\s+)?(?:auditors?|accounting\s+firm)",
        ),
    ),
    _fam(
        T_FRAUD,
        "late filing",
        _w(
            r"(?:delay\w*|postpon\w*|late|miss\w*)\s+(?:(?:its|the|their|our)\s+)?(?:annual|quarterly|10-?K|10-?Q)"
            r"\s+(?:report|filing)",
            r"(?:delay\w*|postpon\w*)\s+(?:(?:its|the|their|our)\s+)?(?:filing\s+of\s+(?:its\s+|the\s+)?)?(?:10-?K|10-?Q)",
            r"fail\w*\s+to\s+file\s+(?:its\s+|the\s+|their\s+)?(?:annual|quarterly|10-?K|10-?Q)",
        ),
    ),
    _fam(T_FRAUD, "late filing notice", _LB + r"NT\s+10-[KQ]" + _RB, cs=True),
    # ---- legal / regulatory
    _fam(
        T_LEGAL,
        "regulator action",
        _LB + _REG_CS + r"\s+(?i:(?:has\s+|have\s+|is\s+|are\s+|just\s+)?"
        r"(?:(?:reportedly|allegedly|now|quietly|officially|formally)\s+)?(?:open(?:s|ed|ing)?|launch\w*|start\w*"
        r"|investigat\w*|prob(?:e|es|ed|ing)|subpoena\w*|su(?:es|ed|ing)|charg(?:es|ed|ing)|fin(?:es|ed)|raid\w*"
        r"|indict\w*|accus\w*|alleg\w*|target\w*|look\w*\s+into|examin\w*|scrutiniz\w*|sanction\w*|block\w*"
        r"|ban(?:s|ned)?|seek\w*\s+to\s+(?:block|ban|halt)))" + _RB,
        cs=True,
    ),
    _fam(
        T_LEGAL,
        "regulator probe",
        _LB + _REG_CS + r"\s+(?i:investigations?|probes?|inquir(?:y|ies)|lawsuits?|suit|complaint|charges"
        r"|subpoenas?|scrutiny|enforcement\s+action|crackdown)" + _RB,
        cs=True,
    ),
    _fam(
        T_LEGAL,
        "investigation",
        _w(
            r"(?:investigations?|probes?|inquir(?:y|ies))\s+into",
            r"under\s+(?:a\s+)?(?:federal\s+|criminal\s+|SEC\s+|DOJ\s+|regulatory\s+|antitrust\s+)?investigation",
            r"subpoena(?:s|ed)?",
            r"wells\s+notices?",
            r"(?:criminal|federal|antitrust|congressional|senate)\s+(?:charges?|investigation|probe|referral|case"
            r"|complaint|lawsuit|suit|inquiry|hearing|subpoena)",
            r"antitrust\s+(?:lawsuit|suit|case|probe|investigation|charges|complaint|action)",
            r"indict(?:ed|ment|ments)",
            r"charged\s+with",
            r"whistle-?blowers?",
        ),
    ),
    _fam(
        T_LEGAL,
        "lawsuit",
        _w(
            r"class[-\s]action",
            r"lawsuits?\s+(?:filed|against|alleg\w*|accus\w*|claims?|seeks?|seeking|over)",
            r"(?:was|were|been|gets?|got|is\s+being|are\s+being)\s+sued",
            r"sued\s+by",
            r"(?:faces?|facing|hit\s+with|slapped\s+with)\s+(?:a\s+|an\s+|new\s+)?" + _GAP2
            + r"(?:lawsuits?|suits?|probes?|investigations?|charges|fines?|class\s+actions?)",
        ),
    ),
    # "FTC files a lawsuit against Amazon" - bad news for the defendant; when the company itself posts "we have
    # filed a lawsuit against ...", it is the plaintiff (see RedFlagScan.event_types)
    _fam(
        T_LEGAL,
        "lawsuit filed",
        _w(
            r"(?:files?|filed|filing|brings?|brought|launch\w*)\s+(?:a\s+|an\s+)?" + _GAP2
            + r"(?:lawsuits?|suits?|complaints?)\s+against"
        ),
        ctx=CTX_PLAINTIFF,
    ),
    _fam(
        T_LEGAL,
        "fine / settlement",
        _w(
            r"fined",
            r"(?:fine|penalt(?:y|ies))\s+of\s+(?:up\s+to\s+)?\$",
            r"settl\w*\s+" + _GAP2 + r"(?:charges|allegations|claims|probe|investigation|fraud)",
            r"consent\s+decree",
            r"cease[-\s]and[-\s]desist",
            r"(?:preliminary|permanent)\s+injunction",
            r"(?:export|import|sales?)\s+ban",
            r"(?:banned|bans?)\s+(?:from|in)\s+(?:the\s+)?(?:US|U\.S\.|EU|China|Europe|selling)",
            r"raid(?:s|ed)?\s+(?:on\s+)?" + _GAP2 + r"(?:offices?|headquarters|HQ|facility|factory)",
        ),
    ),
    # ---- product safety
    _fam(
        T_SAFETY,
        "recall",
        _w(
            r"(?:safety|voluntary|product|vehicle|massive|software|global|nationwide|FDA|NHTSA|OTA|over-the-air"
            r"|mandatory|urgent|class\s+I{1,3}|food|drug|device)\s+recalls?",
            r"recall(?:s|ed|ing)?\s+(?:of\s+)?(?:about\s+|over\s+|nearly\s+|some\s+|all\s+|more\s+than\s+|roughly\s+"
            r"|almost\s+)?(?:\d[\d,.]*\s?(?:[KkMm]|thousand|million)?\s+)?(?:(?:of\s+)?(?:its|their|the|our|every"
            r"|all)\s+)?" + _GAP2 + r"(?:vehicles|cars|trucks|units|products|devices|batteries|cybertrucks|models"
            r"|pills|vials|doses|lots|batches|chargers|phones|laptops|servers|chips|cans|bottles)",
            r"(?:is|are|was|were)\s+recalling",
            r"(?:issu\w*|announc\w*|order\w*|expand\w*|launch\w*|initiat\w*)\s+(?:a\s+|an\s+)?" + _GAP2 + r"recalls?",
            r"recall\s+(?:notice|campaign|order|affecting|covering|covers)",
        ),
    ),
    _fam(
        T_SAFETY,
        "safety defect",
        _w(
            r"safety\s+(?:defects?|issues?|probes?|investigations?|concerns?|problems?|risks?|warnings?|hazards?)",
            r"defect\s+investigation",
            r"warning\s+letters?",
            r"(?:fatal|deadly)\s+(?:crash\w*|accidents?|fires?|explosions?|incidents?)",
            r"crash(?:es|ed)?\s+" + _GAP3 + r"(?:killed|kills|killing|dead|deaths?)",
            r"(?:caught|catches|catching)\s+(?:on\s+)?fire",
            r"battery\s+fires?",
            r"contaminat(?:ed|ion)",
            r"(?:add\w*|new|requir\w*)\s+(?:a\s+)?(?:boxed|black[-\s]box)\s+warnings?",
            r"(?:patient|customer|user|driver|passenger)\s+deaths?",
        ),
    ),
    _fam(T_SAFETY, "safety regulator", _LB + r"(?:NHTSA|CPSC)\s+(?i:opens?|opened|probe\w*|investigat\w*)" + _RB, cs=True),
    # ---- financial warnings (direction: cuts / misses / defaults only)
    _fam(
        T_FINANCIAL,
        "guidance cut",
        _w(
            r"(?:cut|cuts|cutting|lower(?:s|ed|ing)?|slash(?:es|ed|ing)?|trim(?:s|med|ming)?|reduc(?:e|es|ed|ing)"
            r"|withdr(?:aw|aws|ew|awn|awing)|pull(?:s|ed|ing)?|suspend(?:s|ed|ing)?)\s+" + _GUIDANCE_OBJ,
            r"(?:guidance|outlook|forecasts?)\s+(?:cut|lowered|slashed|trimmed|reduced|withdrawn|suspended|pulled)",
            r"(?:below|under)\s+(?:(?:our|its|the|their)\s+)?(?:prior|previous|earlier|own|initial)\s+(?:outlook"
            r"|guidance|forecast|expectations)",
        ),
    ),
    _fam(
        T_FINANCIAL,
        "profit warning",
        _w(
            r"profit\s+warnings?",
            r"warns?\s+(?:(?:of|on|about)\s+)?(?:(?:a|lower|weaker|weak|soft|softer)\s+)?(?:profits?|revenue|sales"
            r"|earnings|results|demand|margins?)",
            r"miss(?:ed|es)?\s+(?:(?:analyst|analysts['’]|Wall\s+Street|street|consensus)\s+)?(?:estimates|expectations"
            r"|consensus|forecasts)",
        ),
    ),
    _fam(
        T_FINANCIAL,
        "bankruptcy / default",
        _w(
            r"bankrupt(?:cy|cies)?",
            r"chapter\s+(?:7|11|15)",
            r"insolven(?:t|cy)",
            r"receivership",
            r"default(?:s|ed)?\s+on",
            r"miss\w*\s+(?:(?:a|an|its|their)\s+)?(?:debt|interest|bond|coupon|loan)\s+payments?",
            r"liquidity\s+(?:crisis|crunch|problems|issues|concerns)",
            r"cash\s+crunch",
            r"(?:going|runs?|running|ran)\s+out\s+of\s+(?:cash|money)",
        ),
    ),
    # The company being delisted, not an exchange delisting a coin ("We're delisting Bitcoin SV" is routine)
    _fam(
        T_FINANCIAL,
        "delisting / trading halt",
        _w(
            r"de-?list(?:ed|ment)",
            r"de-?listing\s+(?:notices?|letters?|warnings?|threats?|risks?|proceedings?|determination|review|process"
            r"|deadline|from|of|by)",
            r"(?:face|faces|facing|risk|risks|risking|avoid|avoids|avoiding|fight\w*|appeal\w*|threat\s+of|risk\s+of"
            r"|possible|potential|imminent|looming)\s+(?:an?\s+|the\s+)?(?:possible\s+|potential\s+)?de-?listing",
            r"(?:move|moves|moved|moving|plan|plans|planned|planning|threaten\w*|decid\w*|vot\w*|seek\w*|order\w*)"
            r"\s+to\s+de-?list",
        ),
    ),
    _fam(
        T_FINANCIAL,
        "delisting / trading halt",
        _w(
            r"trading\s+(?:halt(?:ed)?|suspended|suspension)",
            r"halt(?:s|ed)?\s+trading",
        ),
        ctx=CTX_HALT,
    ),
    _fam(
        T_FINANCIAL,
        "dilution / dividend cut",
        _w(
            r"(?<!non-)(?<!non\s)(?<!anti-)(?<!anti\s)dilut(?:ion|ive)",
            r"(?:cut|cuts|slash\w*|suspend\w*|eliminat\w*|reduc\w*)\s+(?:(?:its|the|our|their)\s+)?(?:quarterly\s+)?"
            r"dividends?",
            r"dividends?\s+(?:cut|suspended|eliminated|slashed)",
            r"reverse\s+(?:stock\s+)?split",
        ),
    ),
    # ---- executives
    _fam(
        T_EXEC,
        "executive exit",
        _w(
            _ROLE + r"\s+" + _GAP3 + r"(?:resign(?:s|ed|ing)?|quit|quits|steps?\s+down|stepped\s+down|stepping\s+down"
            r"|ousted|fired|terminated|departs?|departed|departing|exits?|exited|exiting|placed\s+on\s+(?:administrative\s+)?"
            r"leave|on\s+leave|out\s+as|is\s+out)",
            r"(?:resign(?:s|ed|ing)?|steps?\s+down|stepped\s+down|stepping\s+down|ousted|fired|removed|departs?|exits?)"
            r"\s+(?:as|from\s+(?:the\s+)?(?:role\s+of\s+|position\s+of\s+)?)\s*(?:the\s+)?(?:company['’]s\s+)?" + _ROLE,
            r"(?:sudden|abrupt|unexpected|surprise)(?:ly)?\s+(?:departure|resignation|exit|ouster|firing)",
            r"ousted",
            r"executive\s+(?:exodus|departures)",
            r"(?:resign(?:s|ed)|steps?\s+down|stepped\s+down)\s+(?:effective|immediately|abruptly|suddenly|amid|after"
            r"|over|today)",
            # "CEO Pat Gelsinger retires, effective immediately" (a planned retirement is not a red flag)
            _ROLE + r"\s+" + _GAP3 + r"retir(?:es|ed|ing)\s*,?\s+(?:effective\s+immediately|immediately|abruptly"
            r"|suddenly|unexpectedly|amid)",
            # "Tesla CFO Vaibhav Taneja has left the company"
            _ROLE + r"\s+" + _GAP3 + r"(?:has\s+left|have\s+left|is\s+leaving|left)\s+(?:the\s+company|the\s+firm"
            r"|the\s+board|(?:his|her|their)\s+(?:role|post|position))",
        ),
    ),
    _fam(T_OTHER, "scandal", _w(r"scandals?")),
    _fam(
        T_EXEC,
        "insider selling",
        _w(
            r"insider\s+(?:selling|sales|dumping|sells)",
            _ROLE + r"\s+" + _GAP3 + r"(?:sold|sells|dump(?:s|ed|ing)|unload(?:s|ed|ing)|offload(?:s|ed|ing))\s+"
            r"(?:\$?\d|shares|stock|stake|his|her|their)",
        ),
    ),
    _fam(
        T_EXEC,
        "executive misconduct",
        _w(
            r"(?:sexual\s+)?misconduct",
            r"harassment\s+(?:allegations?|claims?|probe|investigation|lawsuit)",
            _ROLE + r"\s+" + _GAP3 + r"(?:arrested|charged|indicted|under\s+investigation)",
        ),
    ),
    # ---- analyst downgrades
    _fam(
        T_DOWNGRADE,
        "downgrade",
        _w(
            r"downgrad(?:e|es|ed|ing)\s+" + _GAP4 + r"(?:to|at)\s+(?:an?\s+)?(?:sell|underperform|underweight|neutral"
            r"|hold|market\s+perform|sector\s+perform|equal[-\s]?weight|in[-\s]line|reduce|junk|peer\s+perform)",
            r"(?:analysts?|rating|credit|Moody['’]?s|S&P|Fitch)\s+" + _GAP3 + r"downgrad\w*",
            r"downgrad(?:e|es|ed)\s+(?:of\s+)?\$[A-Za-z]{1,6}",
            r"\$[A-Za-z]{1,6}\s+(?:[\w-]+\s+){0,2}?downgrad\w*",
            r"downgraded\s+(?:by|at)",
            r"(?:cut|cuts|slash\w*)\s+(?:to\s+)?junk",
            r"initiat\w+\s+(?:coverage\s+)?(?:of\s+[\w$.-]+\s+)?(?:at|with\s+an?)\s+(?:sell|underperform|underweight)",
            # "BofA cuts Apple to Neutral from Buy" (the rating must end the phrase: not "cuts prices to hold share")
            r"(?:cut|cuts|lower(?:s|ed)?)\s+" + _GAP2 + r"to\s+(?:an?\s+)?(?:sell|underperform|underweight|neutral|hold"
            r"|market\s+perform|sector\s+perform|equal[-\s]?weight|in[-\s]line|reduce|peer\s+perform)"
            r"(?=\s*(?:$|[.,;:!)(]|from\b|rating\b|on\b|at\b|with\b|amid\b|after\b|\$))",
        ),
    ),
    _fam(
        T_DOWNGRADE,
        "price target cut",
        _w(
            r"(?:cut|cuts|lower(?:s|ed)?|slash\w*|reduc\w*|trim\w*)\s+(?:(?:its|the|their|our|a)\s+)?"
            r"(?:\$?[A-Za-z]{1,6}\s+)?(?:price\s+target|target\s+price)",
            r"price\s+target\s+(?:cut|lowered|slashed|reduced|trimmed)",
        ),
    ),
    _fam(
        T_DOWNGRADE,
        "price target cut",
        _LB + r"(?i:cut|cuts|lower(?:s|ed)?|slash\w*|trim\w*)\s+(?:(?i:its|the|their)\s+)?(?:\$?[A-Za-z]{1,6}\s+)?PT"
        + _RB + r"|" + _LB + r"PT\s+(?i:cut|lowered|slashed|reduced)" + _RB,
        cs=True,
    ),
    # ---- security incidents
    _fam(
        T_SECURITY,
        "breach / hack",
        _w(
            r"(?:data|security|privacy|network|system|systems|customer\s+data)\s+breach(?:es)?",
            r"breach(?:ed)?\s+(?:of\s+)?(?:(?:our|their|its|the|customer|user)\s+)?(?:systems?|security|networks?"
            r"|data|servers?|databases?|accounts?|records)",
            r"(?:was|were|been|got|gets|has\s+been|have\s+been)\s+hacked",
            r"hacked\s+(?:accounts?|systems?|servers?|databases?|data|by)",
            r"hack(?:ers?)?\s+(?:stole|accessed|breached|compromised|exposed|leaked)",
            r"(?:massive|major|huge|big)\s+hack",
            r"hacked(?=\s*[.,!:;?]|\s*$)",  # "Coinbase hacked." (not "I hacked together a demo")
            r"cyber-?attacks?",
            r"ransomware",
            r"data\s+leaks?",
            r"leak(?:ed|s)?\s+(?:customer|user|personal|private)\s+(?:data|information|records)",
            r"compromised\s+(?:accounts?|systems?|data|servers?|credentials)",
            r"(?:accounts?|systems?|servers?|data)\s+(?:were|was|been|are)\s+compromised",
        ),
        ctx=CTX_SECURITY,
    ),
    _fam(
        T_SECURITY,
        "outage",
        _w(
            r"outages?",
            r"(?:systems?|site|website|apps?|services?|servers?|platform|network|api|exchange|login|trading)"
            r"\s+(?:is|are|was|were|went|goes|going)\s+(?:currently\s+|completely\s+|partially\s+)?down"
            r"(?!\s*(?:\d|\$|by\b|from\b|to\b|yoy\b|year\b))",
        ),
        ctx=CTX_OUTAGE,
    ),
    # ---- clinical setbacks (an approval / a met endpoint never matches)
    _fam(
        T_CLINICAL,
        "complete response letter",
        _w(r"complete\s+response\s+letters?", r"refus(?:e|al)[-\s]to[-\s]file"),
    ),
    _fam(
        T_CLINICAL,
        "complete response letter",
        r"(?:" + _LB + r"FDA\s+(?i:issu\w*|sends?|sent|hands?|handed)|" + _LB + r"(?i:receiv\w*|gets?|got))\s+"
        r"(?:(?i:an?|the|its|a\s+second)\s+)?(?<!\$)CRL" + _RB
        # "$APMD CRL. Ouch." / "CRL for AD109" (upper-case CRL only; "$CRL" alone is a ticker)
        + r"|" + _LB + r"\$[A-Za-z]{1,6}\s+CRL" + _RB + r"|(?<![$A-Za-z0-9_])CRL\s+(?i:for|on|from)" + _RB,
        cs=True,
    ),
    _fam(T_CLINICAL, "clinical hold", _w(r"clinical\s+holds?", r"partial\s+clinical\s+hold")),
    _fam(
        T_CLINICAL,
        "missed endpoint",
        _w(
            r"(?:fail(?:s|ed|ing)?\s+to\s+(?:meet|achieve|reach|hit)|did(?:\s+not|n['’]t)\s+(?:meet|achieve|reach|hit)"
            r"|does(?:\s+not|n['’]t)\s+meet|miss(?:ed|es|ing)?)\s+(?:(?:its|the|both|all|our|their|key|two)\s+){0,2}"
            + _ENDPOINT,
            _ENDPOINT + r"\s+(?:was|were)\s+not\s+met",
            _TRIAL + r"\s+(?:[\w-]+\s+){0,3}?(?:failed|fails|missed|misses)",
            r"for\s+futility",
            r"(?:negative|disappointing|failed)\s+(?:top-?line|phase\s?[1-4IV]{1,3}[ab]?|pivotal|trial|clinical|interim)"
            r"\s+(?:\w+\s+)?(?:results?|data|read-?outs?)",
            r"safety\s+signal",
        ),
    ),
    _fam(
        T_CLINICAL,
        "trial halted",
        _w(
            r"(?:" + _TRIAL + r"|dosing|(?<!free\s)(?:patient|study|trial)\s+(?:dosing|enrol{1,2}ment))\s+"
            r"(?:(?:has\s+been|have\s+been|was|were|is)\s+)?(?:halted|stopped|discontinued|terminated|paused|suspended)",
            r"(?:halt(?:s|ed|ing)?|discontinu(?:e|es|ed|ing)|terminat(?:e|es|ed|ing)|paus(?:e|es|ed|ing)"
            r"|suspend(?:s|ed|ing)?)\s+(?:(?:the|its|our|a|all)\s+)?" + _GAP2 + r"(?:" + _TRIAL
            + r"|dosing|enrol{1,2}ment)",
        ),
    ),
    _fam(
        T_CLINICAL,
        "FDA rejection",
        _LB + r"(?:FDA|EMA|CHMP)\s+(?i:rejects?|rejected|declines?|declined|denies|denied|refuses?|refused)" + _RB
        + r"|" + _LB + r"(?i:advisory\s+(?:committee|panel)|panel)\s+(?i:vot\w+)\s+(?:\d+\s?(?:-|to)\s?\d+\s+)?"
        r"(?i:against)" + _RB + r"|" + _LB + r"AdCom\s+(?i:vot\w+)\s+(?:\d+\s?(?:-|to)\s?\d+\s+)?(?i:against)" + _RB,
        cs=True,
    ),
)

# A bare "fraud" / "fraudulent": a red flag unless it is anti-fraud marketing or consumer advice ("Visa blocked $40B
# in fraud", "fraud detection", "fight fraud", "fraudulent transactions", "Fraud is on the rise ... ways to stay safe").
_BARE_FRAUD_RE = re.compile(r"(?=[Ff])" + _LB + r"fraud(?:ulent)?" + _RB, re.IGNORECASE)
_FRAUD_MARKETING_AFTER_RE = re.compile(
    r"(?<![-\s,])(?:[-\s]{0,3}(?=[a-z])(?:detection|prevention|protections?|proof|resistant|free|fighting|monitoring|team|teams"
    r"|tools?|controls?|alerts?|rates?|loss(?:es)?|risk|solutions?|scoring|screening|experts?|fighters?|awareness"
    r"|attempts?|cases|transactions?|calls?|texts?|emails?|messages?|charges|activity|scams?|sites?|websites?|links?"
    r"|apps?|ads?|listings?|purchases?|payments?|accounts?|attacks?|rings?|schemes\s+targeting|guard|shield"
    r"|insights?|intelligence|defen[cs]es?|checks?|signals?|trends?|season|labs?|unit|squad|hotline)"
    r"|,?\s{1,3}waste|\s{1,3}(?:is\s|are\s)?\s{0,2}on\s{1,3}the\s{1,3}rise"
    r"|\s{1,3}(?:is\s{1,3})?(?:rising|everywhere))" + _RB,
    re.IGNORECASE,
)
# "fraudulent injury claims", "fraudulent SMS pumping attempts": fraud by others against the company
_FRAUDULENT_BY_OTHERS_AFTER_RE = re.compile(
    r"(?<!\s)\s{1,3}(?:[A-Za-z-]{1,30}\s{1,3}){0,2}?(?:claims|reviews|returns|chargebacks|sellers|actors|orders|refunds|robocalls|sms"
    r"|pumping|traffic|sign-?ups|registrations|bots|activity|transactions|charges|calls|texts|emails|messages"
    r"|accounts|websites|sites|links|apps|ads|listings|purchases|payments|schemes|attempts|requests|users|merchants"
    r"|vendors|brokers|products|goods|copies|knock-?offs|calls|domains)" + _RB,
    re.IGNORECASE,
)
_FRAUD_MARKETING_BEFORE_RE = re.compile(
    _LB + r"(?:block(?:s|ed|ing)?|prevent(?:s|ed|ing)?|stop(?:s|ped|ping)?|fight(?:s|ing)?|fought|combat(?:s|ed|ing)?"
    r"|detect(?:s|ed|ing)?|reduc(?:e|es|ed|ing)|protect(?:s|ed|ing|ion)?|against|anti|tackl(?:e|es|ed|ing)"
    r"|beat(?:s|ing)?|cut(?:s|ting)?|lower(?:s|ed|ing)?|avoid(?:s|ed|ing)?|identif(?:y|ies|ied|ying)|flag(?:s|ged|ging)?"
    r"|safe|secure|AI|scams?|help(?:s|ed|ing)?|root(?:s|ed|ing)?\s+out|signs\s+of|spot(?:s|ting)?|recogni[sz]\w*"
    r"|guard(?:s|ed|ing)?|shield(?:s|ed|ing)?|educat\w*|tips)" + _RB,
    re.IGNORECASE,
)
# consumer advice anywhere in the post: a bare "fraud" there is a warning, not an allegation
_FRAUD_ADVICE_RE = re.compile(
    _LB + r"(?:stay(?:ing)?\s+safe|keep\s+(?:you|your\w*)\s+safe|protect\s+(?:your|yourself|you)|ways\s+to\s+(?:stay"
    r"|spot|avoid|protect|keep)|tips\s+(?:to|for|on)|warning\s+signs|be\s+(?:aware|vigilant|careful|alert)|how\s+to"
    r"\s+(?:spot|avoid|protect|stay|report)|shop\s+safely)" + _RB,
    re.IGNORECASE,
)

# Security incidents and outages in protective, marketing or threat-research context: "stop cyberattacks before
# they spread", "help utilities prevent outages", "zero outages", "74% worry about data breaches", "a new
# ransomware group is targeting hospitals" (checked in the clause before / right after the match).
_GUARD_BEFORE_RE = re.compile(
    _LB + r"(?:prevent\w*|avoid\w*|stop(?:s|ped|ping)?|block\w*|protect\w*|defend\w*|defen[cs]es?|detect\w*"
    r"|against|fight\w*|fought|combat\w*|help\w*|zero|no|fewer|less|cut(?:s|ting)?|reduc\w*|lower\w*"
    r"|minimi[sz]\w*|mitigat\w*|resilien\w*|withstand\w*|without|(?:worr\w*|concern\w*|afraid|fears?)\s+(?:about|of)"
    r"|anti|secur(?:e|es|ing)|safeguard\w*|thwart\w*|guard\w*|shield\w*)" + _RB,
    re.IGNORECASE,
)
_SECURITY_CONTEXT_AFTER_RE = re.compile(
    r"(?<![-\s])[-\s]{1,3}(?:groups?|gangs?|actors?|operators?|strains?|variants?|famil(?:y|ies)|campaigns?|threats?|trends?"
    r"|landscape|protection|defen[cs]es?|prevention|detection|readiness|resilience|insurance|reports?|research"
    r"|risks?|tools?|solutions?|experts?|simulations?|drills?|exercises?|awareness|playbooks?|attempts"
    r"|preparedness)" + _RB,
    re.IGNORECASE,
)
_OUTAGE_CONTEXT_AFTER_RE = re.compile(
    r"(?<![-\s])[-\s]{1,3}(?:protection|prevention|insurance|risks?|management|planning|drills?|simulations?|detection"
    r"|solutions?|tools?)" + _RB,
    re.IGNORECASE,
)
# Scheduled halts and planned maintenance ("CME Globex will halt trading at 12:15 p.m. for the Thanksgiving holiday")
_ROUTINE_RE = re.compile(
    _LB + r"(?:(?:scheduled|planned|routine)\s+(?:maintenance|downtime|outages?|halts?|closures?)|maintenance\s+window"
    r"|for\s{1,3}(?:the\s{1,3})?(?:\w{1,30}\s{1,3})?(?:holiday|thanksgiving|christmas|new\s+year['’]?s?|juneteenth|independence\s+day"
    r"|memorial\s+day|labor\s+day|good\s+friday)|early\s+close|market\s+holiday|holiday\s+(?:schedule|hours|trading))"
    + _RB,
    re.IGNORECASE,
)
_ROUTINE_BEFORE_CHARS = 80
_ROUTINE_AFTER_CHARS = 120

# Positive outcomes that turn a legal / clinical / safety match into good news. Anywhere in the clause before the
# match: "wins the lawsuit", "cleared of fraud", "FDA lifts the clinical hold" ...
_POSITIVE_BEFORE_RE = re.compile(
    _LB + r"(?:wins?|won|winning|defeat(?:s|ed)?|dismiss(?:es|ed)?|clear(?:s|ed)\s+(?:of|in)|resolv(?:e|es|ed)"
    r"|lift(?:s|ed|ing)?|remov(?:e|es|ed)|no\s+longer|survives?|overturn(?:s|ed)?|revers(?:e|es|ed))" + _RB,
    re.IGNORECASE,
)
# ... and verbs that are only positive when they act on the event itself, right before it ("SEC closes its
# investigation", "DOJ drops the probe") - never "shares closed down 20% after the SEC subpoena"
_POSITIVE_TIED_BEFORE_RE = re.compile(
    _LB + r"(?:end(?:s|ed|ing)?|clos(?:e|es|ed|ing)|drop(?:s|ped|ping)?|conclud(?:e|es|ed|ing)|wrap(?:s|ped)\s+up)"
    r"\s{1,3}(?:(?:its|the|their|his|her|an?|this|that|all)\s{1,3})?(?:[A-Za-z-]{1,30}\s{1,3}){0,2}$",
    re.IGNORECASE,
)
# right after the match (within three words, same clause): "lawsuit against Robinhood dismissed", "hold lifted"
_POSITIVE_AFTER_RE = re.compile(
    r"(?<![\w$.&'’\s-])\s{0,3}(?:[\w$.&'’-]{1,40}\s{1,3}){0,3}?(?:(?:was|were|is|are|has\s+been|have\s+been|had\s+been|been|got|gets|now)\s{1,3})?"
    r"(?:dismissed|tossed|thrown\s+out|dropped|closed|ended|resolved|withdrawn|cleared|lifted|removed|overturned"
    r"|reversed)" + _RB,
    re.IGNORECASE,
)
# ... or anywhere in that clause: "SEC closes investigation into Tesla without action"
_POSITIVE_AFTER_ANY_RE = re.compile(
    _LB + r"(?:without\s+(?:any\s+)?(?:action|charges|penalt(?:y|ies)|a\s+fine|findings?)|no\s+(?:further\s+)?action"
    r"|in\s+(?:its|our|their)\s+favou?r)" + _RB,
    re.IGNORECASE,
)
# share-price words: a positive verb next to them is market news, never the event's outcome
_MARKET_WORD_RE = re.compile(_LB + r"(?:shares?|stocks?|price|futures|premarket|index|market)" + _RB, re.IGNORECASE)
# "trial was stopped early for overwhelming efficacy" is good news
_EFFICACY_AFTER_RE = re.compile(
    r"(?<!\s)\s{0,3}(?:early\s{1,3})?(?:for|due\s+to|because\s+of|after\s+showing)\s{1,3}(?:(?:overwhelming|clear|positive|strong"
    r"|compelling)\s+)?(?:efficacy|benefit)" + _RB,
    re.IGNORECASE,
)
_POSITIVE_AFTER_CHARS = 50
_POSITIVE_TYPES = frozenset({T_LEGAL, T_CLINICAL, T_SAFETY})

# Questions, conditions, denials and recaps right before a match (same clause): a hint, not an event ("if the FDA
# issues a CRL", "we almost went bankrupt", "back in 2008"). "will" is not a hedge: "Supermicro will restate its
# financials" is an announcement; neither is "once again" or "minutes ago".
_HEDGE_BEFORE_RE = re.compile(
    _LB + r"(?:if|whether|what\s+if|might|should|imagine|hypothetical(?:ly)?|den(?:y|ies|ied|ying)|false|fake|untrue"
    r"|almost|nearly|once(?!\s+(?:again|more))|remember|history|back\s+in|(?:years?|months?|decades?)\s+ago"
    r"|last\s+(?:year|decade))" + _RB
    + r"|(?<![A-Za-z])(?-i:may)(?![A-Za-z])",  # lowercase "may" only: "May 5" is a date
    re.IGNORECASE,
)
# "would" / "could" hedge ("Supermicro could face a delisting") unless they report a decision ("said it would recall")
_MODAL_RE = re.compile(_LB + r"(?:would|could)" + _RB, re.IGNORECASE)
_REPORTED_MODAL_RE = re.compile(
    _LB + r"(?:said|says|say|announced|announces|confirmed|confirms|told|tells|stated|states|expects?|expected"
    r"|warned|warns|added|noted|disclosed|discloses|reported|reports)\s+(?:(?:that|it|they|the\s+company|he|she|we)"
    r"\s+){0,2}(?:would|could)" + _RB,
    re.IGNORECASE,
)
# negations count only close to the match ("Tesla is not under investigation"), not "No one is talking about this"
_NEGATION_RE = re.compile(
    _LB + r"(?:no(?!\s+(?:one|doubt|wonder|surprise|matter|joke|kidding|cap|lie)" + _RB + r")|not(?!\s+(?:only|just"
    r"|surprisingly)" + _RB + r")|never|without|[a-z]{1,8}n['’]t)" + _RB,
    re.IGNORECASE,
)
_NEGATION_TOKENS = 6
# "rival Novo Nordisk cuts guidance": bad news for someone else (a hint for Claude, never a rules-only red flag)
_RIVAL_BEFORE_RE = re.compile(_LB + r"(?:rivals?|competitors?|competing|peers?)" + _RB, re.IGNORECASE)
# ... and right after it, in the same clause (at most _RECAP_AFTER_CHARS): "went bankrupt back in 2008".
_RECAP_AFTER_RE = re.compile(
    _LB + r"(?:back\s+in|(?:years?|months?|decades?)\s+ago|last\s+(?:year|decade))" + _RB,
    re.IGNORECASE,
)
# "in 2019" is a recap; "its third recall in <this year>" is not
_YEAR_RE = re.compile(_LB + r"in\s+((?:19|20)\d\d)" + _RB, re.IGNORECASE)
_RECAP_AFTER_CHARS = 40
_HEDGE_BEFORE_CHARS = 50
_HEDGE_AFTER_CHARS = 80
_SENTENCE_BREAK = ".!;\n"
# clause breaks for hedges: a sentence end, a colon or a spaced dash ("If you hold $TSLA: NHTSA opened a probe",
# "Hindenburg is short $SMCI - what now?")
_CLAUSE_BEFORE_RE = re.compile(r"[.!;\n:]|\s[-–—]\s")
_CLAUSE_BREAK_AFTER_RE = re.compile(r"[.!\n;:]|\s[-–—]\s")
_CLAUSE_END_RE = re.compile(r"[.;!?\n]")
_CLAUSE_END_COMMA_RE = re.compile(r"[.;!?\n,:]|\s[-–—]\s")
# first person in the sentence of a match: a company / CEO account talking about itself ("we", "our", "I'm")
_FIRST_PERSON_RE = re.compile(
    r"(?<![A-Za-z0-9_'’])(?:[Ww]e(?:['’](?:re|ve|ll|d))?|[Oo]ur|[Uu]s|I(?:['’](?:m|ve|ll|d))?|[Mm]y)(?![A-Za-z0-9_])"
)
_FIRST_PERSON_BEFORE_CHARS = 80
_FIRST_PERSON_AFTER_CHARS = 60


def _clip(text: str | None) -> str:
    if not text:
        return ""
    return text if len(text) <= MAX_TEXT_CHARS else text[:MAX_TEXT_CHARS]


def _sentence_before(text: str, start: int, chars: int) -> str:
    before = text[max(0, start - chars) : start]
    cut = max(before.rfind(c) for c in _SENTENCE_BREAK)
    return before[cut + 1 :] if cut >= 0 else before


def _clause_before(text: str, start: int, chars: int) -> str:
    """At most ``chars`` characters before ``start``, from the last clause break (sentence end, ':' or ' - ')."""
    before = text[max(0, start - chars) : start]
    last = None
    for m in _CLAUSE_BEFORE_RE.finditer(before):
        last = m
    return before[last.end() :] if last is not None else before


def _clause_after(text: str, end: int, chars: int, breaks: re.Pattern[str]) -> str:
    """At most ``chars`` characters after ``end``, cut at the first clause break."""
    after = text[end : end + chars]
    cut = breaks.search(after)
    return after[: cut.start()] if cut else after


def _old_year(fragment: str, year: int) -> bool:
    """'in 2019' (a year before ``year``) in the fragment: a recap of old news."""
    return any(int(m.group(1)) < year for m in _YEAR_RE.finditer(fragment))


def _hedged(text: str, start: int, end: int, year: int) -> bool:
    """A question / condition / negation / denial / recap / rival framing around ``text[start:end]``."""
    before = _clause_before(text, start, _HEDGE_BEFORE_CHARS)
    if _HEDGE_BEFORE_RE.search(before) or _RIVAL_BEFORE_RE.search(before) or _old_year(before, year):
        return True
    reported = {m.end() for m in _REPORTED_MODAL_RE.finditer(before)}
    if any(m.end() not in reported for m in _MODAL_RE.finditer(before)):
        return True
    if _NEGATION_RE.search(" ".join(before.split()[-_NEGATION_TOKENS:])):
        return True
    recap = _clause_after(text, end, _RECAP_AFTER_CHARS, _CLAUSE_END_COMMA_RE)
    if _RECAP_AFTER_RE.search(recap) or _old_year(recap, year):
        return True
    # a question only when the matched clause itself is the question ("Is Tesla under SEC investigation?")
    after = text[end : end + _HEDGE_AFTER_CHARS]
    cut = _CLAUSE_BREAK_AFTER_RE.search(after)
    return "?" in (after[: cut.start()] if cut else after)


def _positive_outcome(text: str, start: int, end: int) -> bool:
    """'wins the lawsuit', 'SEC closes its investigation', 'clinical hold lifted', 'trial stopped early for efficacy':
    good news, not a red flag. Share-price moves ('shares closed down 3%') never count."""
    before = _clause_before(text, start, _HEDGE_BEFORE_CHARS)
    for m in _POSITIVE_BEFORE_RE.finditer(before):
        if not _MARKET_WORD_RE.search(before[max(0, m.start() - 25) : m.start()]):
            return True
    tied = _POSITIVE_TIED_BEFORE_RE.search(before)
    if tied is not None and not _MARKET_WORD_RE.search(before[max(0, tied.start() - 25) :]):
        return True
    after = _clause_after(text, end, _POSITIVE_AFTER_CHARS, _CLAUSE_END_COMMA_RE)
    m = _POSITIVE_AFTER_RE.match(after)
    if m is not None and not _MARKET_WORD_RE.search(m.group(0)):
        return True
    return _POSITIVE_AFTER_ANY_RE.search(after) is not None or _EFFICACY_AFTER_RE.match(text, end) is not None


def _fraud_is_marketing(text: str, start: int, end: int) -> bool:
    if _FRAUD_MARKETING_AFTER_RE.match(text, end):
        return True
    if text[start:end].lower() == "fraudulent" and _FRAUDULENT_BY_OTHERS_AFTER_RE.match(text, end):
        return True
    if _FRAUD_MARKETING_BEFORE_RE.search(_sentence_before(text, start, 40)) is not None:
        return True
    return _FRAUD_ADVICE_RE.search(text) is not None


def _guarded(text: str, start: int, end: int, ctx: str) -> bool:
    """A security / outage match in protective, marketing or research context (see _GUARD_BEFORE_RE)."""
    if _GUARD_BEFORE_RE.search(_clause_before(text, start, 40)):
        return True
    after_rx = _SECURITY_CONTEXT_AFTER_RE if ctx == CTX_SECURITY else _OUTAGE_CONTEXT_AFTER_RE
    return after_rx.match(text, end) is not None


def _routine(text: str, start: int, end: int) -> bool:
    """A scheduled halt / planned maintenance around the match (same line)."""
    lo, hi = max(0, start - _ROUTINE_BEFORE_CHARS), min(len(text), end + _ROUTINE_AFTER_CHARS)
    nl = text.rfind("\n", lo, start)
    lo = nl + 1 if nl >= 0 else lo
    nl = text.find("\n", end, hi)
    hi = nl if nl >= 0 else hi
    return _ROUTINE_RE.search(text[lo:hi]) is not None


def _first_person(text: str, start: int, end: int) -> bool:
    """'we' / 'our' / 'I' in the sentence of the match (the author talks about itself)."""
    before = _sentence_before(text, start, _FIRST_PERSON_BEFORE_CHARS)
    after = _clause_after(text, end, _FIRST_PERSON_AFTER_CHARS, _CLAUSE_END_RE)
    return _FIRST_PERSON_RE.search(before) is not None or _FIRST_PERSON_RE.search(after) is not None


# --------------------------------------------------------------------------- scan result


@dataclass(frozen=True)
class RedFlagHit:
    type: str
    label: str
    hedged: bool  # framed as a question / condition / negation / recap / about a rival: a hint only
    plaintiff: bool = False  # "X filed a lawsuit against ...": bad news for the defendant, not for a company posting it
    first_person: bool = False  # "we" / "our" / "I" in the match's sentence (the author talks about itself)


@dataclass(frozen=True)
class RedFlagScan:
    """Everything the red-flag regexes found in one text (fixed labels only, never post text)."""

    hits: tuple[RedFlagHit, ...] = ()
    denial: tuple[str, ...] = ()
    rumor: tuple[str, ...] = ()

    @property
    def types(self) -> list[str]:
        """Unhedged red-flag types, in RED_FLAG_TYPES order."""
        found = {h.type for h in self.hits if not h.hedged}
        return [t for t in RED_FLAG_TYPES if t in found]

    @property
    def hedged_types(self) -> list[str]:
        """Types that only matched in a hedged way (question, condition, negation)."""
        strong = {h.type for h in self.hits if not h.hedged}
        found = {h.type for h in self.hits if h.hedged and h.type not in strong}
        return [t for t in RED_FLAG_TYPES if t in found]

    @property
    def labels(self) -> list[str]:
        return list(dict.fromkeys(h.label for h in self.hits if not h.hedged))

    @property
    def is_red_flag(self) -> bool:
        return bool(self.types) and not self.denial

    def event_types(self, *, by_company: bool = False) -> list[str]:
        """Unhedged types; ``by_company`` (a company / CEO account posting): without "X filed a lawsuit against"
        matches, which make the poster the plaintiff."""
        found = {h.type for h in self.hits if not h.hedged and not (by_company and h.plaintiff)}
        return [t for t in RED_FLAG_TYPES if t in found]

    @property
    def first_person_types(self) -> list[str]:
        """Unhedged, non-plaintiff types matched in a first-person sentence ("we are recalling", "our auditor")."""
        found = {h.type for h in self.hits if not h.hedged and not h.plaintiff and h.first_person}
        return [t for t in RED_FLAG_TYPES if t in found]


def _scan_red_flags(text: str, year: int) -> tuple[RedFlagHit, ...]:
    hits: list[RedFlagHit] = []
    seen: set[RedFlagHit] = set()

    def add(hit: RedFlagHit) -> None:
        if hit not in seen:
            seen.add(hit)
            hits.append(hit)

    matches = [(fam, m) for fam in _FAMILIES for m in itertools.islice(fam.rx.finditer(text), MAX_MATCHES_PER_PATTERN)]
    # "X filed lawsuits against Y" also matches the plain "lawsuits against": both are the plaintiff form
    plaintiff_spans = [(m.start(), m.end()) for fam, m in matches if fam.ctx == CTX_PLAINTIFF]
    for fam, m in matches:
        s, e = m.start(), m.end()
        if fam.type in _POSITIVE_TYPES and _positive_outcome(text, s, e):
            continue
        if fam.ctx in (CTX_SECURITY, CTX_OUTAGE) and _guarded(text, s, e, fam.ctx):
            continue
        if fam.ctx in (CTX_HALT, CTX_OUTAGE) and _routine(text, s, e):
            continue
        plaintiff = fam.ctx == CTX_PLAINTIFF or (
            fam.type == T_LEGAL and any(ps < e and s < pe for ps, pe in plaintiff_spans)
        )
        hedged = _hedged(text, s, e, year)
        add(RedFlagHit(fam.type, fam.label, hedged, plaintiff, not hedged and _first_person(text, s, e)))
    for m in itertools.islice(_BARE_FRAUD_RE.finditer(text), MAX_MATCHES_PER_PATTERN):
        s, e = m.start(), m.end()
        if _fraud_is_marketing(text, s, e):
            continue
        hedged = _hedged(text, s, e, year)
        add(RedFlagHit(T_FRAUD, "fraud allegation", hedged, False, not hedged and _first_person(text, s, e)))
    return tuple(hits)


# --------------------------------------------------------------------------- denials

_DENIAL_PATTERNS: tuple[tuple[str, Pattern[str]], ...] = tuple(
    (label, re.compile(p, re.IGNORECASE))
    for label, p in (
        ("fake news", _w(r"fake\s+news")),
        (
            "categorically false",
            _w(
                r"(?:categorically|completely|totally|patently|entirely|simply|absolutely|demonstrably|100\s?%)\s+"
                r"(?:false|untrue|fake|wrong|inaccurate|fabricated|baseless|made\s+up)"
            ),
        ),
        (
            "says the report is false",
            _w(
                r"(?:this|that|it|the\s+(?:report|story|article|claim|rumou?r|post|tweet|screenshot|letter|headline"
                r"|news)|these\s+(?:reports|claims|rumou?rs|stories|allegations|posts)|the\s+allegations|such\s+reports"
                r"|reports|claims|rumou?rs|allegations)\s+(?:(?:is|are|was|were)\s+)(?:(?:simply|completely|totally"
                r"|categorically|entirely|absolutely|just|patently)\s+)?(?:false|untrue|not\s+true|fake|wrong|incorrect"
                r"|fabricated|baseless|inaccurate|misleading|unfounded|bogus|nonsense|made\s+up)",
                # "The short seller report published today is false and misleading", "these claims are without merit"
                r"(?:report|story|article|claim|rumou?r|allegation|accusation|assertion|post|tweet|headline|letter"
                r"|screenshot)s?\s+" + _GAP4 + r"(?:(?:is|are|was|were)\s+)(?:(?:simply|completely|totally"
                r"|categorically|entirely|absolutely|just|patently|both)\s+)?(?:false|untrue|not\s+true|fake|wrong"
                r"|incorrect|fabricated|baseless|inaccurate|misleading|unfounded|bogus|nonsense|made\s+up"
                r"|without\s+(?:any\s+)?merit|meritless)",
                r"(?:they|these|those|which|such\s+claims)\s+are\s+(?:(?:completely|totally|entirely|simply)\s+)?"
                r"(?:false|baseless|unfounded|without\s+(?:any\s+)?merit|meritless|untrue|fabricated)",
            ),
        ),
        ("no truth to it", _w(r"no\s+truth\s+(?:to|in|whatsoever)", r"not\s+true", r"(?:is|are)\s+untrue")),
        ("baseless", _w(r"baseless", r"unfounded", r"without\s+(?:any\s+)?merit", r"meritless")),
        (
            "rejects the claims",
            _w(
                # ("Court rejects Tesla's claims" is not a denial: "rejects" needs "we" or an emphatic adverb)
                r"(?:(?:(?<=we\s)|(?<=I\s)|(?<=categorically\s)|(?<=firmly\s)|(?<=strongly\s)|(?<=completely\s)"
                r"|(?<=fully\s)|(?<=vigorously\s)|(?<=flatly\s)|(?<=emphatically\s))reject(?:s|ed)?"
                r"|refut(?:e|es|ed|ing)|rebut(?:s|ted|ting)?)\s+"
                r"(?:(?:the|these|this|those|all|any|such|today['’]s|recent)\s+)?" + _GAP4
                + r"(?:claims?|allegations?|reports?|accusations?|assertions?|rumou?rs?|stor(?:y|ies)|articles?)",
                r"(?:we|I|the\s+company)\s+(?:firmly\s+|fully\s+)?stand(?:s)?\s+(?:by|behind)\s+(?:our|its|the)\s+"
                r"(?:financials|financial\s+statements|results|reporting|accounting|numbers|disclosures|audits?)",
            ),
        ),
        (
            "denies the report",
            _w(
                r"den(?:y|ies|ied|ying)\s+(?:(?:the|these|this|any|all|recent|such|those|media|today['’]s)\s+)?"
                r"(?:\w+\s+)?(?:reports?|rumou?rs?|claims?|allegations?|stor(?:y|ies)|articles?|accusations?)"
            ),
        ),
        (
            "doctored / fake document",
            _w(
                r"(?:fabricated|doctored|photoshopped|forged|manipulated|fake)\s+(?:screenshots?|images?|documents?"
                r"|letters?|press\s+releases?|posts?|tweets?|videos?|memos?|emails?|filings?|headlines?)"
            ),
        ),
        (
            "denies the event",
            _w(
                r"(?:has|have|had)\s+not\s+(?:received|been\s+(?:contacted|notified|subpoenaed|charged))",
                r"(?:is|are)\s+not\s+(?:under\s+(?:any\s+)?investigation|recalling|being\s+investigated|being\s+sued"
                r"|bankrupt|insolvent|halting|delisting|resigning|stepping\s+down)",
                r"no\s+(?:such\s+|pending\s+|ongoing\s+)?(?:investigation|probe|recall|lawsuit|subpoena|inquiry|halt"
                r"|resignation|short\s+position)",
                r"not\s+aware\s+of\s+any",
            ),
        ),
        ("misinformation", _w(r"misinformation", r"disinformation", r"debunk(?:s|ed|ing)?", r"hoax(?:es)?")),
        ("never happened", _w(r"never\s+happened", r"(?:not|in)accurate\s+report(?:s|ing)?")),
        # a post that starts with a one-word verdict: "False.", "Fake. Tesla is not recalling ...", and replies, whose
        # text starts with X's "@handle " prefix: "@WSJ False" (the pipeline still needs a tracked target to deny)
        (
            "one-word denial",
            r"^\W{0,5}(?:@\w{1,15}\s+){0,10}\W{0,5}(?:fake|false|untrue|not\s+true|wrong|nope|lies?|totally\s+false)"
            r"(?![A-Za-z0-9_])(?!\s+(?:positive|alarm|start|flag|teeth|dawn))",
        ),
    )
)


def _scan_denial(text: str) -> tuple[str, ...]:
    return tuple(label for label, rx in _DENIAL_PATTERNS if rx.search(text))


# --------------------------------------------------------------------------- rumor markers

_RUMOR_PATTERNS: tuple[tuple[str, Pattern[str]], ...] = tuple(
    (label, re.compile(p, re.IGNORECASE))
    for label, p in (
        ("rumor", _w(r"rumou?r(?:s|ed)?")),
        ("unconfirmed", _w(r"unconfirmed", r"unverified", r"not\s+(?:yet\s+)?(?:confirmed|verified)")),
        ("reportedly", _w(r"reportedly")),
        (
            "anonymous sources",
            _w(
                r"sources?\s+(?:say|said|says|tell|told|claim|familiar)",
                r"according\s+to\s+(?:a\s+)?(?:sources?|people|person|insiders?)",
                r"(?:people|person)\s+familiar",
                r"insiders?\s+(?:say|said|says)",
            ),
        ),
        (
            "hearing that",
            _w(
                r"(?:I['’]m|we['’]re|I\s+am|we\s+are|been|keep)\s+hearing",
                r"hearing\s+(?:that|rumou?rs|reports|chatter|whispers)",
            ),
        ),
        ("allegedly", _w(r"allegedly")),
        ("leaked", _w(r"leak(?:ed|s)?")),
        ("cannot confirm", _w(r"(?:can['’]t|cannot|could\s+not|couldn['’]t)\s+(?:confirm|verify)")),
        ("whispers", _w(r"whispers?", r"chatter")),
    )
)
_BREAKING_RE = re.compile(r"(?<![A-Za-z])BREAKING(?![A-Za-z])")


def _scan_rumor(text: str, has_link: bool) -> tuple[str, ...]:
    found = [label for label, rx in _RUMOR_PATTERNS if rx.search(text)]
    if not has_link and _BREAKING_RE.search(text):
        found.append("all-caps BREAKING without a link")
    return tuple(found)


# --------------------------------------------------------------------------- scan (cached)


@functools.lru_cache(maxsize=512)
def _scan_cached(text: str, has_link: bool, year: int) -> RedFlagScan:
    clipped = _clip(text)
    return RedFlagScan(
        hits=_scan_red_flags(clipped, year),
        denial=_scan_denial(clipped),
        rumor=_scan_rumor(clipped, has_link),
    )


def scan(text: str | None, *, has_link: bool = False, year: int | None = None) -> RedFlagScan:
    """Red-flag hits, denial phrases and rumor markers in ``text`` (capped at MAX_TEXT_CHARS).

    ``year`` (default: the current UTC year) decides what is a recap: "in 2019" is old news, "its third recall in
    <this year>" is not.
    """
    return _scan_cached(text or "", bool(has_link), int(year) if year is not None else datetime.now(UTC).year)


def detect_red_flags(text: str | None) -> list[str]:
    """Unhedged negative-event types (values from RED_FLAG_TYPES) in ``text``; [] if none or a denial."""
    s = scan(text)
    return s.types if not s.denial else []


def detect_denial(text: str | None) -> list[str]:
    """Denial phrases ("fake news", "categorically false", "no truth to", "denies the report", ...)."""
    return list(scan(text).denial)


def rumor_markers(text: str | None, *, has_link: bool = False) -> list[str]:
    """Rumor markers (rumor, unconfirmed, reportedly, sources say, hearing that, allegedly, leaked,
    all-caps BREAKING without a link)."""
    return list(scan(text, has_link=has_link).rumor)


# --------------------------------------------------------------------------- hijacked accounts

_EVM_ADDRESS_RE = re.compile(r"(?<![0-9A-Za-z])0x[0-9a-fA-F]{40}(?![0-9A-Za-z])")
# base58 (no 0, O, I, l): Solana-style addresses, 32-44 characters. The look-behind kills every start inside a run.
_B58 = "1-9A-HJ-NP-Za-km-z"
_BASE58_RE = re.compile(rf"(?<![{_B58}0OIl])[{_B58}]{{32,44}}(?![{_B58}0OIl])")
# bech32 bitcoin addresses (bc1...; the bech32 alphabet has no 1, b, i, o after the prefix)
_BECH32_RE = re.compile(r"(?<![0-9A-Za-z])bc1[02-9ac-hj-np-z]{25,62}(?![0-9A-Za-z])")
# Memecoin trading sites, as DOMAINS only (never a bare word or an @handle such as @dextools_fan, SW-5)
_MEME_LINK_RE = re.compile(
    r"(?<![@A-Za-z0-9_-])(?:pump\.fun|dexscreener\.com|dextools\.io|birdeye\.so|raydium\.io|jup\.ag"
    r"|photon-sol\.[a-z]{2,12}|gmgn\.ai|bullx\.io|moonshot\.money|four\.meme|sunpump\.meme)(?![A-Za-z0-9-])",
    re.IGNORECASE,
)
# @mentions are blanked before the scan: a reply's "@airdrop_fan" prefix is not scam content
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])@\w{1,15}")
# Classic giveaway-scam phrases: on their own enough to call a watched account possibly hijacked
_COMPROMISE_STRONG: tuple[tuple[str, Pattern[str]], ...] = tuple(
    (label, re.compile(p, re.IGNORECASE))
    for label, p in (
        (
            "send-and-get-back scam",
            _w(
                r"double\s+your\s+(?:crypto|bitcoin|BTC|ETH|SOL|money|coins?)",
                r"send\s+(?:me\s+|us\s+)?(?:\d[\d,.]*\s*)?(?:ETH|BTC|SOL|USDT|USDC|crypto|bitcoin|ethereum|solana)\s+"
                + _GAP4 + r"(?:and|&)\s+(?:I['’]ll|we['’]ll|I\s+will|we\s+will|(?:you['’]ll|you\s+will)\s+"
                r"(?:get|receive))\s+(?:send\s+)?(?:[\w.$-]+\s+){0,3}?back",
                r"send\s+" + _GAP3 + r"to\s+(?:this|my|our|the\s+following)\s+(?:address|wallet)",
            ),
        ),
    )
)
# Hype phrases. Each is ordinary product language on its own ("You can now send crypto to friends", "claim your
# SoFi rewards", "we're launching the cbBTC token"), so they only count together, or next to an address / link.
_COMPROMISE_WEAK: tuple[tuple[str, Pattern[str]], ...] = tuple(
    (label, re.compile(p, re.IGNORECASE))
    for label, p in (
        ("send-crypto request", _w(r"send\s+(?:me\s+|us\s+)?(?:\d[\d,.]*\s*)?(?:ETH|BTC|SOL|USDT|USDC|crypto|bitcoin"
                                   r"|ethereum|solana)")),
        (
            "token launch",
            _w(
                r"(?:launch(?:ed|ing|es)?|releas(?:e|ed|ing)|drop(?:ped|ping)?|introduc(?:e|ed|ing))\s+"
                r"(?:our|my|the\s+official|its|an\s+official|the)\s+(?:own\s+|new\s+|official\s+)?(?:[\w$-]+\s+)?"
                r"(?:token|coin|memecoin|meme\s+coin)",
            ),
        ),
        ("airdrop", _w(r"air-?drops?")),
        ("presale", _w(r"pre-?sales?")),
        ("claim now", _w(r"claim\s+(?:your|yours|now|here|it\s+now|free|the\s+(?:\$?[A-Za-z]{2,10}\s+)?(?:airdrop"
                         r"|tokens?|rewards?))")),
        ("giveaway", _w(r"give-?aways?", r"giving\s+away", r"give\s+away")),
        ("connect wallet", _w(r"connect\s+(?:your\s+)?wallets?")),
        ("contract address", _w(r"contract\s+address")),
        ("memecoin", _w(r"meme\s?coins?")),
        ("100x", _w(r"(?:100|1000|1,000)x")),
        ("official token", _w(r"official\s+(?:\$?[A-Za-z]{2,10}\s+)?(?:token|memecoin|meme\s+coin|coin)")),
        ("free crypto", _w(r"free\s+(?:crypto|bitcoin|BTC|ETH|SOL|tokens?)")),
        ("NFT mint", _w(r"NFTs?", r"mint\s+(?:yours|your|now|it|one)", r"free\s+mint")),
        ("eligibility check", _w(r"eligib(?:le|ility)")),
    )
)
_CA_RE = re.compile(r"(?<![A-Za-z])CA\s*[:：]")
# A scam WARNING ("we will never ask you to send crypto", "beware of fake airdrops") is not a scam.
_SCAM_WARNING_RE = re.compile(
    _w(
        r"beware",
        r"scam(?:s|mers)?",
        r"phishing",
        r"impersonat\w+",
        r"never\s+ask",
        r"will\s+never",
        r"(?:do\s+not|don['’]t|never)\s+send",
        r"not\s+affiliated",
        r"no\s+official",
        r"there\s+is\s+no",
        r"fake",
    ),
    re.IGNORECASE,
)
#: links to these hosts are X itself (quote / media links), never a phishing link
_X_HOSTS = frozenset({"x.com", "twitter.com", "t.co", "pic.twitter.com", "mobile.twitter.com", "pbs.twimg.com"})
#: a brand token must be at least this long to count in a look-alike domain ("meta", "base" are too common)
MIN_BRAND_CHARS = 5
_BRAND_RE = re.compile(r"[^a-z0-9]")
_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


@dataclass(frozen=True)
class CompromiseCheck:
    """Scam-like crypto content (the classic hijacked-account memecoin post)."""

    signals: tuple[str, ...] = ()  # fixed labels, never post text
    likely: bool = False  # strong enough to call the account possibly hijacked
    # an address next to hype, a memecoin trading link or a send-and-get-back scam: no ordinary post looks like this.
    # A likely but not definitive check (hype words, a look-alike link) is confirmed with Claude when Claude scored.
    definitive: bool = False


def brand_tokens(names: Iterable[str | None]) -> tuple[str, ...]:
    """Lower-case alphanumeric brand tokens (handles, display names, company names) of at least MIN_BRAND_CHARS."""
    out: list[str] = []
    for n in names:
        t = _BRAND_RE.sub("", str(n or "").lstrip("=").lower())
        if len(t) >= MIN_BRAND_CHARS and t not in out:
            out.append(t)
    return tuple(out)


def _host(url: str) -> str | None:
    candidate = url.strip()
    if "://" not in candidate:
        candidate = "https://" + candidate
    try:
        host = urlsplit(candidate).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    host = host[4:] if host.startswith("www.") else host
    return host if len(host) <= 253 and _DOMAIN_RE.match(host) else None


def _link_kinds(urls: tuple[str, ...], brands: tuple[str, ...]) -> tuple[bool, bool]:
    """(look-alike, other): a link whose domain contains a brand but is not that brand's own domain
    (coinbase-airdrop.xyz), and any other link that is neither X itself nor the brand's own site."""
    lookalike = other = False
    for url in urls[:MAX_MATCHES_PER_PATTERN]:
        host = _host(url)
        if host is None or host in _X_HOSTS or any(host.endswith("." + x) for x in _X_HOSTS):
            continue
        labels = host.split(".")
        core = labels[-2] if len(labels) >= 2 else labels[0]
        if core in brands:
            continue  # coinbase.com, help.coinbase.com: the brand's own site
        other = True
        if any(b in label for b in brands for label in labels[:-1]):
            lookalike = True
    return lookalike, other


@functools.lru_cache(maxsize=512)
def _compromise_cached(text: str, urls: tuple[str, ...], brands: tuple[str, ...]) -> CompromiseCheck:
    t = _MENTION_RE.sub(lambda m: " " * len(m.group(0)), _clip(text))
    blob = "\n".join([t, *urls])
    signals: list[str] = []
    address = False
    if _EVM_ADDRESS_RE.search(blob):
        signals.append("EVM wallet / contract address")
        address = True
    for m in itertools.islice(_BASE58_RE.finditer(blob), MAX_MATCHES_PER_PATTERN):
        s = m.group(0)
        if any(c.isdigit() for c in s) and any(c.isupper() for c in s) and any(c.islower() for c in s):
            signals.append("Solana-style address")
            address = True
            break
    if _BECH32_RE.search(blob):
        signals.append("bitcoin address")
        address = True
    meme_link = _MEME_LINK_RE.search(blob) is not None
    if meme_link:
        signals.append("memecoin trading link")
    lookalike, other_link = _link_kinds(urls, brands)
    if lookalike:
        signals.append("look-alike link domain")
    strong = [label for label, rx in _COMPROMISE_STRONG if rx.search(t)]
    weak = [label for label, rx in _COMPROMISE_WEAK if rx.search(t)]
    if _CA_RE.search(t):
        weak.append("contract address")
    signals.extend(strong)
    signals.extend(w for w in dict.fromkeys(weak) if w not in signals)
    # "contract address" alone is how crypto CEOs share legit addresses: it only labels, never decides
    hype = {w for w in weak if w != "contract address"}
    if _SCAM_WARNING_RE.search(t) is not None:
        likely = definitive = address and bool(strong or meme_link)
    else:
        definitive = bool(strong) or meme_link or (address and bool(hype))
        likely = definitive or (lookalike and bool(hype)) or (other_link and len(hype) >= 2) or len(hype) >= 3
    return CompromiseCheck(signals=tuple(dict.fromkeys(signals)), likely=likely, definitive=definitive)


def detect_compromise(
    text: str | None, *, urls: Iterable[str] = (), brands: Iterable[str] = ()
) -> CompromiseCheck:
    """Scam signals in a watched account's own words and links: wallet addresses, memecoin trading links, look-alike
    link domains (``brands``: the account's brand tokens, see :func:`brand_tokens`), send-and-get-back scams and
    hype phrases (airdrop, presale, giveaway, claim now, connect wallet, NFT mint, token launch ...).

    ``likely``: a definitive signal (an address next to hype, a memecoin link, a send-and-get-back scam), a
    look-alike link plus one hype phrase, another link plus two, or three hype phrases. Ordinary product language
    ("send USDC to anyone", "claim your SoFi rewards", "launching the cbBTC token") is not enough on its own. A scam
    *warning* ("we will never ask you to send crypto") needs an address plus a scam phrase or memecoin link.
    """
    return _compromise_cached(
        text or "",
        tuple(str(u) for u in itertools.islice(urls, MAX_MATCHES_PER_PATTERN) if u),
        tuple(brands),
    )


#: every compiled pattern (for the linear-time tests)
ALL_PATTERNS: tuple[Pattern[str], ...] = (
    *(f.rx for f in _FAMILIES),
    _BARE_FRAUD_RE,
    _FRAUD_MARKETING_AFTER_RE,
    _FRAUDULENT_BY_OTHERS_AFTER_RE,
    _FRAUD_MARKETING_BEFORE_RE,
    _FRAUD_ADVICE_RE,
    _GUARD_BEFORE_RE,
    _SECURITY_CONTEXT_AFTER_RE,
    _OUTAGE_CONTEXT_AFTER_RE,
    _ROUTINE_RE,
    _POSITIVE_BEFORE_RE,
    _POSITIVE_TIED_BEFORE_RE,
    _POSITIVE_AFTER_RE,
    _POSITIVE_AFTER_ANY_RE,
    _MARKET_WORD_RE,
    _EFFICACY_AFTER_RE,
    _HEDGE_BEFORE_RE,
    _MODAL_RE,
    _REPORTED_MODAL_RE,
    _NEGATION_RE,
    _RIVAL_BEFORE_RE,
    _RECAP_AFTER_RE,
    _YEAR_RE,
    _CLAUSE_BEFORE_RE,
    _CLAUSE_BREAK_AFTER_RE,
    _CLAUSE_END_RE,
    _CLAUSE_END_COMMA_RE,
    _FIRST_PERSON_RE,
    *(rx for _, rx in _DENIAL_PATTERNS),
    *(rx for _, rx in _RUMOR_PATTERNS),
    _BREAKING_RE,
    _EVM_ADDRESS_RE,
    _BASE58_RE,
    _BECH32_RE,
    _MEME_LINK_RE,
    _MENTION_RE,
    *(rx for _, rx in _COMPROMISE_STRONG),
    *(rx for _, rx in _COMPROMISE_WEAK),
    _CA_RE,
    _SCAM_WARNING_RE,
)


def type_label(red_flag_type: str) -> str:
    return TYPE_LABELS.get(red_flag_type, red_flag_type.replace("_", " "))


__all__ = [
    "ALL_PATTERNS",
    "MAX_TEXT_CHARS",
    "MIN_BRAND_CHARS",
    "TYPE_LABELS",
    "CompromiseCheck",
    "RedFlagHit",
    "RedFlagScan",
    "brand_tokens",
    "detect_compromise",
    "detect_denial",
    "detect_red_flags",
    "rumor_markers",
    "scan",
    "type_label",
]
