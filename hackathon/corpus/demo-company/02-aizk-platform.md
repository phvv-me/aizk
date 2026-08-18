# AIZK platform architecture

AIZK means AI Zettelkasten. It gives agents a shared memory with source-grounded find, organization
boundaries, durable files, and a web interface.

The demonstration stores documents, chunks, vector embeddings, graph claims, and organization scope
sets in CockroachDB Cloud. A C-SPANN vector index supplies semantic candidates. Indexed lexical
retrieval supplies exact matches. Reciprocal rank fusion combines those lanes before AIZK renders an
answer with evidence. Database functions carry verified caller authority into each transaction and
enforce complete scope matching for every tenant row.

AWS Lambda runs the public web application, MCP and API requests, and bounded background jobs. Amazon
S3 preserves uploaded originals. Logto provides sign-in, organizations, roles, and agent OAuth.
OpenRouter supplies text embeddings and extraction for the small demonstration corpus.

Maya Chen can write to Northstar Engineering and Northstar Works. She can read Northstar Product and
Northstar Research without changing their memory. Ordinary users cannot see operator or external
service controls.

All people and organizations in this note are fictional demonstration data.
