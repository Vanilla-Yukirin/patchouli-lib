# Product positioning: a durable knowledge library

## One sentence

PatchouliLib is a network-accessible, self-hostable knowledge backend that lets
people and software agents write, retrieve, revise, and cite shared knowledge
without turning private infrastructure into a product assumption.

## Intended users

- Individuals who use several tools or agents against one knowledge base.
- Small teams that need auditable, scoped access to shared text knowledge.
- Tool authors building CLI, MCP, or agent-skill adapters.
- Researchers evaluating retrieval and knowledge-organization strategies.

## Goals

- Provide one durable home for notes, project records, conversation archives,
  research material, and other text-first sources.
- Preserve original content and revision history while keeping current reads
  simple.
- Make citations stable enough for both humans and agents.
- Support layered retrieval: structure, metadata, full text, summaries, and
  optional derived indexes.
- Keep access controlled, attributable, and exportable.

## Non-goals for the first supported release

- A hosted public multi-tenant service.
- General-purpose file synchronization.
- Binary media storage and processing.
- A model gateway or model-routing platform.
- Unreviewed autonomous rewriting, moving, or deletion of source content.
- A mandatory vector database, model provider, cloud provider, or web UI.

## Deployment posture

The public project defines portable service contracts and safe defaults. Each
operator chooses where and how to run the service. Examples must use synthetic
hosts and placeholder credentials; a contributor's real deployment topology is
not part of the public design.

## Content posture

All text content is represented as a Page, regardless of whether it began as a
note, configuration explanation, project document, conversation archive, web
capture, or paper analysis. Content types may add validation and presentation,
but they do not create separate storage silos.

In this design, an **archive** means a preserved agent or assistant conversation.
It is one content type, not a synonym for every stored document.
