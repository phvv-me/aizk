---
title: "DataHub"
description: "What a metadata context platform shares with a memory engine, and what it does not."
---

DataHub is an open source context platform for a data and AI stack, built at LinkedIn and Apache
2.0. It calls itself a unified context graph, which sounds close enough to what aizk does to be
worth reading properly rather than dismissing. Most of it turns out to be a different problem, and
three parts are worth keeping.

```text
  DataHub                              aizk
  ─────────────────────────────        ─────────────────────────────
  catalogs assets a team owns          remembers what people wrote and said
  entity = URN + versioned aspects     content and claim split by who and when
  lineage between assets               provenance from a claim to its sentence
  policies and roles                   scopes under forced row security
  ingests from Snowflake, Tableau      ingests documents, pages, conversations
```

## What does not transfer

Most of the surface area. The connectors for Snowflake, BigQuery, Redshift, Tableau, Looker and
Power BI catalogue warehouse assets, and aizk has no warehouse. Domains, data products, business
glossaries and ownership types organise a team's tables, where aizk organises what a person knows.
The deployment and cloud material is theirs. Reading it as a feature list to chase would be a
mistake, because the shared word is context and the shared problem is not.

## The aspect model, and why aizk splits differently

Their entity is a URN plus a set of aspects that version independently. Foreign keys inside the
aspects declare relationships that can be followed in either direction. The reason
aspects version separately is that different facets of an asset are updated by different
producers at different times, so one schema change should not invalidate an ownership record.

aizk splits on a different seam. Content is what is asserted, and a claim is who asserted it,
inside which scopes, over which interval of validity. That split exists because the same sentence
can be claimed by two people who disagree, and because a claim can stop being true without the
content changing. DataHub's seam is between facets of one asset, aizk's is between an assertion
and its standing, and neither generalises into the other. Worth knowing when someone asks why
aizk does not simply adopt an established metadata model.

## Their incident model is thinner than aizk's ingestion pipeline

This is the useful finding, and it arrived while designing agent authored bug reports for aizk.

DataHub tracks incidents against assets with two states, active and resolved, and two fields,
title and description, raised through the API, the UI or an automation, and resolved with a
message. That is a good fit for a catalogue where an incident is an operational flag on a table.

That is less information than a report gains through aizk's ordinary path. A stored report becomes
searchable chunks with extracted entities and facts. Each result retains provenance to the exact
sentence. Its history records a fix as the end of a validity range, while statement kind preserves
whether the report was certain or tentative. Recurrence then becomes a query over history rather
than a field somebody must remember to set.

So the conclusion for aizk is to extract reports rather than store them flat, and DataHub is the
evidence for it rather than the model to copy.

## The naming collision, which will bite

In DataHub's documentation MCP means Metadata Change Proposal, the event that carries a metadata
write through their system, and it appears throughout their architecture pages. DataHub also
provides a Model Context Protocol server. Both meanings live in the same docs under the same three
letters, and aizk uses the second exclusively. Anyone reading their architecture material beside
ours needs to know that, because the two MCPs are unrelated and the confusion is silent.

## Worth copying, cheaply

`llms.txt`. DataHub publishes one at `docs.datahub.com/llms.txt`, a single plain page that names
every documentation entry point with a sentence about each so an agent can find the right page
without crawling it. aizk publishes documentation to the same readers and does not have one. It
costs one generated page and it makes the docs answerable by the agents that are the primary
audience, which is a better return than any feature on their list.

Their agent surface is worth watching for a second reason. DataHub exposes its metadata to
assistants over MCP and provides an agent context kit with prebuilt skills. A client can use
DataHub for warehouse metadata and aizk for memory as separate MCP servers. This is a
compatibility observation, not planned integration work.

## Sources

This comparison uses `docs.datahub.com/llms.txt`, the metadata model page, and the incidents page.
Features identified as Cloud only in their documentation are not considered here.
