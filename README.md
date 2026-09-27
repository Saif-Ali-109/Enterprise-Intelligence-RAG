# Enterprise Knowledge Intelligence RAG

An evidence-grounded RAG system over publicly available Atlassian documentation. It ingests
pages by URL at run time, answers only from retrieved evidence, cites every substantive
claim with a link back to the original page, and refuses to answer when the evidence is thin.
Citations are validated in code, never trusted to the model.

**Status:** design phase complete — no application code yet. See
[`specs/001-enterprise-knowledge-rag/`](specs/001-enterprise-knowledge-rag/).

Independent technical demonstration. Not affiliated with, sponsored by, or endorsed by
Atlassian or any documentation publisher. No third-party page content is stored in this
repository.

MIT licensed — see [LICENSE](LICENSE).
