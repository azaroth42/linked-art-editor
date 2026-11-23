"""Bulk bootstrap: load records into Postgres, export them as RDF, build the QLever index.

The first load of a large collection does not go through the publish path and the outbox (spec
§3.3, §12; Phase 1 Step 1.12; ADR-0030). It runs as ``lae bootstrap`` commands, each printing a
JSON summary on stdout:

``load --input <dir-or-jsonl>`` (``lae.bootstrap.load``)
    Reads Linked Art JSON-LD, repairs ids, gives every record that comes with a URI from
    elsewhere a new ``<base_uri><uuidv7>`` URI (the old one goes to ``equivalent`` and to
    ``record_aliases``), assigns internal node IRIs, and writes ``records`` (version 1) and
    their ``entity_labels`` rows with ``COPY``. A record that fails JSON Schema is loaded and
    flagged in ``qa_findings``; one that cannot be expanded to RDF is rejected and listed in the
    report file. A second pass (``lae.bootstrap.rewrite``) rewrites the references to re-minted
    records and strips ``_label`` from references whose target is known.
``rewrite-refs`` (``lae.bootstrap.rewrite``)
    Runs that second pass over every bulk-loaded record: after an interrupted load, after
    loading labels, or after a later load brought in records that older ones refer to.
``export-nt --out <file.nt>`` (``lae.bootstrap.export``)
    Expands every active record with ``lae.rdf.expand_to_ntriples`` in a process pool into one
    file, and adds the labels of sidecar entities (``entity_labels.source != 'local'``) as quads
    in ``urn:graph:sidecar:<source>``. Records that fail expansion are reported and skipped.
``build-index --nt <file>`` (``lae.bootstrap.index``)
    Builds a QLever index from that file with ``deploy/Qleverfile`` in a fresh directory, starts
    the server with ``--persist-updates``, and records the index watermark in ``sync_state``.
``aat-labels --csv <file>`` (``lae.bootstrap.aat``)
    Seeds ``entity_labels`` with Getty AAT terms, the Phase 1 stand-in for the AAT sidecar.

Measured throughput and the reasons behind the design are in ``docs/spikes/S6-bootstrap.md``.
"""
