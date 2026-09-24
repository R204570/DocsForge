"""
A topic, as a rule for which pages of a manual belong to a harvest.

"Go for web development" is not all of Go's documentation and not three
pages of it either: it is every page of the manual that is about building for
the web, whole, plus the pages that introduce the language so those make
sense. That is what `learn_technology(topic=...)` harvests, and what it
leaves out it says, by section, so a caller who wanted more can widen.

The judgement is arithmetic a person can follow, because a relevance rule
nobody can read is a rule nobody can correct: the topic's words, widened by a
small vocabulary of what each is usually called in documentation, counted in a
page's title, its address, its headings and its prose. The terms used are
reported with the harvest.

Choosing pages is scope, never a coverage claim about the pages not chosen:
what is stored is measured against what the topic selected, and what it did
not select is declared, not dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

#: What each topic word is usually called in documentation: the words that
#: name the subject itself first, then the ones that merely travel with it.
#: The split matters. "json", "url" and "request" turn up on every page of a
#: language's release notes, and a first cut that weighed them like "http"
#: and "router" kept 26 Go release notes as web development (2026-09-24).
LEXICON: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "web": (("web", "http", "https", "server", "servers", "handler", "handlers", "router",
             "routing", "route", "routes", "middleware", "html", "template", "templates",
             "websocket", "websockets", "rest", "restful", "endpoint", "endpoints",
             "net/http", "httptest", "cookie", "cookies", "cors", "webapp", "webserver"),
            ("request", "requests", "response", "responses", "json", "url", "urls", "form",
             "forms", "session", "sessions", "api", "tls", "frontend", "backend", "ssr",
             "client", "headers", "status")),
    "http": (("http", "https", "request", "response", "client", "server", "headers"),
             ("status", "fetch", "cookie", "url")),
    "api": (("api", "apis", "rest", "restful", "endpoint", "endpoints", "graphql", "grpc",
             "openapi"), ("request", "response", "sdk", "client", "http")),
    "auth": (("auth", "authentication", "authorization", "login", "logout", "oauth", "oidc",
              "jwt", "signin", "sign-in", "signup", "sso", "rbac", "permissions"),
             ("token", "tokens", "session", "sessions", "password", "passwords",
              "credentials", "identity", "security")),
    "database": (("database", "databases", "db", "sql", "query", "queries", "orm",
                  "migration", "migrations", "transaction", "transactions", "postgres",
                  "postgresql", "mysql", "sqlite"),
                 ("schema", "index", "indexes", "model", "models", "table", "tables")),
    "testing": (("test", "tests", "testing", "mock", "mocks", "mocking", "fixture",
                 "fixtures", "e2e", "unit-test", "unittest"),
                ("assert", "assertion", "benchmark", "benchmarks", "coverage",
                 "integration")),
    "deploy": (("deploy", "deployment", "deploying", "docker", "container", "containers",
                "kubernetes", "hosting", "serverless"),
               ("production", "build", "builds", "ci", "cd", "edge", "release")),
    "streaming": (("stream", "streams", "streaming", "sse", "server-sent", "websocket",
                   "realtime", "real-time"), ("chunk", "chunks", "async", "events")),
    "cli": (("cli", "command-line", "subcommand", "subcommands", "flags", "terminal"),
            ("command", "commands", "flag", "shell", "args", "arguments")),
    "ui": (("ui", "component", "components", "styling", "layout", "layouts", "theme",
            "themes", "frontend"), ("render", "rendering", "style", "css", "view", "views")),
    "agent": (("agent", "agents", "multi-agent", "tool", "tools", "memory", "graph",
               "graphs", "workflow", "workflows", "human-in-the-loop", "interrupt",
               "interrupts", "checkpoint", "checkpointer", "persistence", "subgraph"),
              ("state", "node", "nodes", "edge", "edges", "stream", "streaming")),
    "concurrency": (("concurrency", "concurrent", "goroutine", "goroutines", "channel",
                     "channels", "mutex", "thread", "threads", "parallel", "parallelism",
                     "async", "await"), ("sync", "worker", "workers", "lock", "locks")),
    "security": (("security", "secure", "csrf", "xss", "vulnerability", "vulnerabilities",
                  "crypto", "cryptography", "encryption", "secrets", "injection"),
                 ("auth", "tls", "sanitize", "permissions")),
    "performance": (("performance", "optimize", "optimization", "optimizing", "profiling",
                     "pprof", "benchmark", "benchmarks", "caching"),
                    ("profile", "cache", "memory", "latency", "speed")),
    "error": (("error", "errors", "exception", "exceptions", "panic", "recover", "retry",
               "retries"), ("failure", "failures", "handling")),
    "config": (("config", "configuration", "configure", "settings"),
               ("options", "env", "environment", "variables")),
    "data": (("dataframe", "dataframes", "csv", "parquet", "serialization", "serialize",
              "deserialize"), ("data", "json", "io", "read", "write", "transform")),
    "mobile": (("mobile", "ios", "android", "react-native", "flutter"),
               ("app", "apps", "device", "devices", "native")),
    "ml": (("ml", "machine-learning", "training", "train", "inference", "dataset",
            "datasets", "tensor", "tensors"), ("model", "models", "embedding", "embeddings")),
    "llm": (("llm", "llms", "prompt", "prompts", "chat", "completion", "completions",
             "embedding", "embeddings", "rag", "retrieval"),
            ("model", "models", "token", "tokens", "tool", "tools", "agent", "agents",
             "streaming")),
    "storage": (("storage", "upload", "uploads", "bucket", "buckets", "blob", "blobs", "s3"),
                ("file", "files", "object", "objects")),
    "messaging": (("queue", "queues", "messaging", "pubsub", "pub/sub", "kafka", "broker"),
                  ("message", "messages", "event", "events", "subscribe", "publish")),
}

#: Words a topic is phrased with that say nothing about which pages it wants.
STOPWORDS = frozenset((
    "for", "the", "a", "an", "with", "in", "on", "of", "to", "and", "or", "using", "use",
    "used", "development", "develop", "developing", "dev", "programming", "docs",
    "documentation", "case", "cases", "usecase", "usecases", "how", "about", "my", "our",
    "building", "build", "making", "creating", "writing", "work", "working", "stuff",
    "things", "app", "application", "applications", "guide", "guides",
))

#: Spellings a caller uses for a lexicon entry.
SYNONYMS = {
    "webdev": "web", "website": "web", "websites": "web", "webapp": "web",
    "webapps": "web", "server-side": "web", "backend": "web", "frontend": "ui",
    "authentication": "auth", "authorization": "auth", "login": "auth",
    "databases": "database", "db": "database", "sql": "database", "orm": "database",
    "test": "testing", "tests": "testing", "deployment": "deploy", "deploying": "deploy",
    "hosting": "deploy", "stream": "streaming", "realtime": "streaming",
    "agents": "agent", "multi-agent": "agent", "errors": "error", "exceptions": "error",
    "configuration": "config", "settings": "config", "apis": "api", "rest": "api",
    "graphql": "api", "ai": "llm", "llms": "llm", "rag": "llm", "chatbot": "llm",
    "chatbots": "llm", "concurrent": "concurrency", "async": "concurrency",
    "goroutines": "concurrency", "files": "storage", "uploads": "storage",
    "queues": "messaging", "events": "messaging", "machine-learning": "ml",
}

#: Pages that introduce the technology. Kept whatever the topic, because
#: the pages chosen for it assume them.
FOUNDATIONAL = re.compile(
    r"(^|[\s/_-])(intro|introduction|overview|getting[\s_-]?started|get[\s_-]?started|"
    r"quick[\s_-]?start|quickstart|install|installation|installing|setup|set[\s_-]?up|"
    r"first[\s_-]steps|basics|fundamentals|concepts|core[\s_-]concepts|tour)"
    r"([\s/_.-]|$)", re.I)

#: A page that lists changes rather than explaining a subject: it names every
#: subject once, so it belongs to a topic only when its title or address
#: says so.
_CHANGES = re.compile(
    r"(release[\s_-]?notes?|changelog|what'?s[\s_-]new|/go1(\.\d+)*|/releases?/|"
    r"history|/news/|upgrad(e|ing)[\s_-]guide|migration[\s_-]guide)", re.I)

_WORD = re.compile(r"[a-z0-9][a-z0-9/+#.-]*")


def vocabulary(topic: str) -> tuple[list[str], list[str]]:
    """The words a topic is looked for by: `(core, related)`, widened through
    `LEXICON`. A word the lexicon does not know is core on its own."""
    words = [w.strip(".-/") for w in _WORD.findall((topic or "").lower())]
    core: list[str] = []
    related: list[str] = []
    for word in words:
        if not word or word in STOPWORDS:
            continue
        key = SYNONYMS.get(word, word)
        core.append(word)
        if key in LEXICON:
            core.append(key)
            core.extend(LEXICON[key][0])
            related.extend(LEXICON[key][1])
    core = list(dict.fromkeys(t for t in core if t and t not in STOPWORDS))
    related = [t for t in dict.fromkeys(related) if t not in core]
    return core, related


def terms(topic: str) -> list[str]:
    """Every word a topic is looked for by, core first."""
    core, related = vocabulary(topic)
    return core + related


def _words(text: str) -> list[str]:
    return [w.strip(".-/") for w in _WORD.findall((text or "").lower())]


@dataclass
class Selector:
    """Decides, page by page, what belongs to a topic, and keeps the account."""

    topic: str
    vocabulary: list[str] = field(default_factory=list)
    kept: int = 0
    left: list[tuple[str, str]] = field(default_factory=list)   # (url, title)
    #: What a page has to score to belong. A title naming the subject clears
    #: it alone; an address naming it needs a little of the prose behind it;
    #: the prose alone only when its headings keep returning to it.
    threshold: float = 6.0

    def __post_init__(self) -> None:
        self.core, self.related = vocabulary(self.topic)
        if not self.vocabulary:
            self.vocabulary = self.core + self.related

    @property
    def active(self) -> bool:
        return bool(self.core)

    def _hits(self, words: set[str]) -> tuple[int, int]:
        return len(words & set(self.core)), len(words & set(self.related))

    def score(self, title: str, url: str, text: str = "") -> float:
        path = urlparse(url).path
        path_words = set(_words(path.replace("-", " ").replace("_", " "))) | set(_words(path))
        title_core, title_rel = self._hits(set(_words(title)))
        url_core, url_rel = self._hits(path_words)
        s = 6.0 * title_core + 2.0 * title_rel + 4.0 * url_core + 1.0 * url_rel
        if _CHANGES.search(title or "") or _CHANGES.search(path):
            # A list of changes names everything; only its own title or
            # address can say it is about this.
            return s if (title_core or url_core) else 0.0
        if text:
            head = text[:120_000]
            headings = set(_words(" ".join(re.findall(r"^#{1,4}\s+(.+)$", head, re.M))))
            h_core, h_rel = self._hits(headings)
            s += min(6.0, 2.0 * h_core + 0.5 * h_rel)
            body = _words(head)
            if body:
                core, related = set(self.core), set(self.related)
                c = sum(1 for w in body if w in core)
                r = sum(1 for w in body if w in related)
                s += min(4.0, 1000.0 * c / len(body) / 3.0)
                s += min(1.0, 1000.0 * r / len(body) / 15.0)
        return s

    def foundational(self, title: str, url: str) -> bool:
        if _CHANGES.search(title or "") or _CHANGES.search(urlparse(url).path or ""):
            return False
        return bool(FOUNDATIONAL.search(title or "") or
                    FOUNDATIONAL.search(urlparse(url).path or ""))

    def wants(self, title: str, url: str, text: str = "") -> bool:
        """Whether this page belongs, and the account updated either way."""
        if not self.active:
            return True
        if self.foundational(title, url) or self.score(title, url, text) >= self.threshold:
            self.kept += 1
            return True
        self.left.append((url, title))
        return False

    def may_want(self, title: str, url: str) -> bool:
        """Before fetching: worth fetching to find out? Lenient on purpose --
        a page is judged properly once its prose is in hand."""
        if not self.active:
            return True
        return self.foundational(title, url) or self.score(title, url) > 0

    def left_by_section(self, most: int = 12) -> list[tuple[str, int]]:
        """What was left out, grouped by the first two path segments."""
        groups: dict[str, int] = {}
        for url, _title in self.left:
            parts = [p for p in urlparse(url).path.split("/") if p]
            key = "/" + "/".join(parts[:2]) + ("/" if parts else "")
            groups[key] = groups.get(key, 0) + 1
        return sorted(groups.items(), key=lambda kv: -kv[1])[:most]
