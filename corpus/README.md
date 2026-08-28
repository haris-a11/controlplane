# Corpus — two governance tiers, deliberately

The Round 2 reference parameters assume *"a mix of well-governed and loosely
governed internal data sources feeding these AI systems."* That mix is modelled
here rather than assumed, because it produces a failure mode no groundedness
score can see: **an answer can be perfectly faithful to the source it was given
and still be wrong, because the source should not have been trusted.**

```
corpus/
├── governed/     authoritative, versioned, owned
└── ungoverned/   real, internal, searchable — and stale
```

`rag.py` stamps every chunk with its tier. `router.py` reads the tier of the
span that actually supported the answer, and each policy profile decides what to
do about it (`ungoverned_action` in `policies.toml`).

## `governed/`

`sample-policy.txt` — Meridian Health Assurance individual mediclaim policy,
version 4.2. A fictional insurer with illustrative wording. This is the answer
key's ground truth. Replace it with the document used in the Round 1 demo before
recording the video.

## `ungoverned/`

Two files of the kind that end up in an enterprise index by accident — internal,
plausible, searchable, unowned, and out of date.

| File | What it is | Where it contradicts the policy |
|---|---|---|
| `intranet-wiki-export.txt` | Unofficial ops-wiki FAQ, no owner, last edited 14 months ago | Maternity 12 months (policy §3.4: **24**) · cataract has no waiting period (§3.2: **24 months**) · room rent flat 3% (§4.1: **1% / 2%** with proportionate deduction under §4.3) · claim settlement 45 days (§7.3: **30**) · emergency treatment abroad reimbursed (§5.3: **excluded**) · a "wellness benefit" covering routine dental (**does not exist**; §5.2 excludes dental) · planned pre-auth 24h (§7.1: **48h**) |
| `support-macros.txt` | Canned support replies from a shared drive, undated | Waiting period "about a month" for most conditions (§3.2: **24 months** for specified illnesses) · pre-existing 24 months (§3.3: **36**) · room rent 3% · ICU "no sub-limit" (§4.2: **5%**) · reimbursement window 30 days (§7.2: **15**) · adventure sports covered (§5.4: **excluded**) · a guaranteed claim reversal (**no such provision**) |

The contradictions are listed here, not in the files themselves — the files have
to read as ordinary internal documents for retrieval to treat them that way.

**The cataract row is the important one.** `RESULTS.md` documents a wrong answer
that scored 0.90 for groundedness and was released by every profile: the question
asserted a false premise, the model agreed, and the agreement echoed language the
source supported. The wiki page above is where a model would plausibly get that
belief. Groundedness cannot catch it. The governance tier of the supporting span
can.

## Adding your own

Drop `.txt` or `.pdf` into either directory. Files placed directly in `corpus/`
are treated as `ungoverned` — untagged provenance is not a reason to trust
something.
