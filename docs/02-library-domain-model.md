# Domain model: Library, Section, Book, Page, and Revision

## Overview

```text
Library
├── Section
│   └── Book
│       └── Page
│           └── Revision
├── Tag
├── Shelf
├── Source
└── Derived Fact
```

## Library

A Library is one logical knowledge service. The first implementation may expose
one Library per deployment, but the data model should not make future export or
multiple-library tooling impossible.

## Section

A Section is a stable policy and navigation boundary. Examples include projects,
research, journals, or conversation archives.

- Every Book belongs to exactly one Section.
- Queries should normally select a Section before searching.
- A Section can define retention, authorization, indexing, and organization
  policy.
- Sections are durable categories, not transient search results.

## Book

A Book is a stable context container.

- A Page belongs to exactly one Book at a time.
- The target Book must exist before a Page can be created.
- Moving a Page changes its Book membership without copying or rewriting its
  Revision history.
- Book creation is inexpensive, but existing Book identity remains stable.
- Split and merge operations are explicit, auditable workflows.

## Page

A Page is the smallest independently addressable document. Proposed fields
include:

- stable ID and human-readable title;
- owning Book and content type;
- lifecycle status and current Revision;
- summary, Tags, and Source metadata;
- created and updated timestamps.

## Revision

A Revision is an immutable Page body plus change metadata. The Page points to
its current Revision; old Revisions remain addressable through explicit history
operations. See [03-page-revision-and-history.md](03-page-revision-and-history.md).

## Tag

Tags are many-to-many retrieval hints. They do not replace Section or Book
membership, and they must not be treated as an authorization mechanism unless
the access-control design explicitly evaluates them.

Tag recommendation may be automated. Applying a recommended Tag is a separate,
auditable write.

## Shelf

A Shelf is a saved projection over Books. It owns no source content. Deleting a
Shelf cannot delete a Book or Page. Manual versus automatically maintained
Shelves remains an open product decision.

## Source

Source records describe provenance such as an original URL, import identifier,
or capture time. Source metadata must not silently grant permission to copy or
redistribute the referenced material.

## Derived Fact

A Derived Fact is a short, rebuildable statement extracted from one or more
Pages. It keeps explicit source links and is never the canonical replacement
for its source Revisions.

## Relational sketch

```text
sections(id, name, description, policy, created_at, updated_at)
books(id, section_id, name, summary, created_at, updated_at)
pages(id, book_id, stable_id, title, type, status,
      summary, current_revision_id, created_at, updated_at, deleted_at)
revisions(id, page_id, number, content_md, message,
          actor_id, created_at)
tags(id, name)
page_tags(page_id, tag_id)
sources(id, page_id, kind, locator, captured_at)
facts(id, content, generation, created_at)
fact_sources(fact_id, page_id, revision_id)
shelves(id, name, query_spec, updated_at)
audit_events(id, actor_id, action, resource_type, resource_id, created_at)
```

This sketch is not a migration or implementation contract. Types, constraints,
and indexes require a public storage proposal.
