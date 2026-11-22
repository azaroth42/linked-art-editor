"""Bulk bootstrap: load records into Postgres, export them as RDF, build the QLever index.

The first load of a large collection does not go through the publish path and the outbox (spec
§3.3, §12; Phase 1 Step 1.12). It runs as four ``lae bootstrap`` commands, each printing a JSON
summary on stdout:

``load --input <dir-or-jsonl>`` (``lae.bootstrap.load``)
    Reads Linked Art JSON-LD, repairs ids, validates with JSON Schema only, assigns internal node
    IRIs, and writes ``records`` (version 1) and their ``entity_labels`` rows with ``COPY``.
    Records that cannot be loaded are written to a report file and skipped.
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
