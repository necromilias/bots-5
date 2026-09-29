# Mandatory design → implementation continuation

Parcel-v1 is immutable once the first worker launches and contains **no tracked
implementation authority**.

After the final design oracle, stop for Mick. If Mick accepts the exact design and
grants implementation authority:

1. preserve his exact text verbatim;
2. create a retained continuation/amendment record referencing:
   - old campaign record hash;
   - old parcel manifest hash;
   - exact accepted design-seal identity;
   - exact Mick adjudication;
3. create successor campaign record and immutable parcel-v2;
4. copy/reference the accepted mutation fence and locked design into parcel-v2;
5. update only the successor record's permissions/target/candidate state;
6. run v0.3 structural + semantic + record/parcel/prompt consistency checks;
7. render fresh implementation prompts from parcel-v2.

Never edit parcel-v1 in place. If a later human adjudication changes task-facing
authority, repeat this process with parcel-v3, etc.

See `IMPLEMENTATION_ROUTING.md`.
