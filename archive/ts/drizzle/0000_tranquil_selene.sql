-- Extensions first: the halfvec and bm25vector column types below need them at CREATE
-- TABLE time, and drizzle-kit has no declarative extension entity.
CREATE EXTENSION IF NOT EXISTS vchord CASCADE;--> statement-breakpoint
CREATE EXTENSION IF NOT EXISTS pg_trgm;--> statement-breakpoint
CREATE EXTENSION IF NOT EXISTS pg_tokenizer CASCADE;--> statement-breakpoint
CREATE EXTENSION IF NOT EXISTS vchord_bm25 CASCADE;--> statement-breakpoint
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;--> statement-breakpoint
CREATE TYPE "public"."watermark_kind" AS ENUM('fact_count', 'raptor_fact_count', 'scorecard');--> statement-breakpoint
CREATE TABLE "chunk" (
	"embedding" halfvec(1024),
	"created_by" uuid NOT NULL,
	"scopes" uuid[] DEFAULT '{}'::uuid[] NOT NULL,
	"id" uuid PRIMARY KEY NOT NULL,
	"document_id" uuid NOT NULL,
	"ord" integer NOT NULL,
	"text" text NOT NULL,
	"lexical" text,
	"tokens" integer,
	"provenance" jsonb DEFAULT '{}'::jsonb NOT NULL,
	"tsv" "tsvector" GENERATED ALWAYS AS (to_tsvector('english', coalesce("chunk"."lexical", "chunk"."text"))) STORED,
	"bm25" "bm25vector",
	"processed_at" timestamp with time zone
);
--> statement-breakpoint
ALTER TABLE "chunk" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
CREATE TABLE "community" (
	"embedding" halfvec(1024),
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	"created_by" uuid NOT NULL,
	"scopes" uuid[] DEFAULT '{}'::uuid[] NOT NULL,
	"id" uuid PRIMARY KEY NOT NULL,
	"label" text NOT NULL,
	"summary" text NOT NULL,
	"member_ids" uuid[] DEFAULT '{}'::uuid[] NOT NULL
);
--> statement-breakpoint
ALTER TABLE "community" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
CREATE TABLE "document" (
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	"created_by" uuid NOT NULL,
	"scopes" uuid[] DEFAULT '{}'::uuid[] NOT NULL,
	"id" uuid PRIMARY KEY NOT NULL,
	"kind" varchar NOT NULL,
	"title" varchar,
	"source_uri" varchar,
	"content_hash" varchar NOT NULL,
	"promoted_from" uuid,
	CONSTRAINT "uq_document_source_scope" UNIQUE("source_uri","scopes")
);
--> statement-breakpoint
ALTER TABLE "document" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
CREATE TABLE "entity_claim" (
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	"created_by" uuid NOT NULL,
	"scopes" uuid[] DEFAULT '{}'::uuid[] NOT NULL,
	"id" uuid PRIMARY KEY NOT NULL,
	"content_id" uuid NOT NULL,
	"attributes" jsonb DEFAULT '{}'::jsonb NOT NULL,
	CONSTRAINT "uq_entity_claim_content_scope" UNIQUE("content_id","scopes")
);
--> statement-breakpoint
ALTER TABLE "entity_claim" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
CREATE TABLE "entity_content" (
	"id" uuid PRIMARY KEY NOT NULL,
	"name" text NOT NULL,
	"type" text NOT NULL,
	"embedding" halfvec(1024)
);
--> statement-breakpoint
ALTER TABLE "entity_content" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
CREATE TABLE "entity_kind" (
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	"name" text PRIMARY KEY NOT NULL,
	"description" text NOT NULL,
	"domain" text NOT NULL,
	"structural" boolean DEFAULT false NOT NULL
);
--> statement-breakpoint
CREATE TABLE "fact_claim" (
	"created_by" uuid NOT NULL,
	"scopes" uuid[] DEFAULT '{}'::uuid[] NOT NULL,
	"id" uuid PRIMARY KEY NOT NULL,
	"content_id" uuid NOT NULL,
	"valid" "tstzrange",
	"recorded" "tstzrange" DEFAULT tstzrange(now(), NULL::timestamp with time zone, '[)'::text) NOT NULL,
	"last_accessed" timestamp with time zone,
	"access_count" integer DEFAULT 0 NOT NULL,
	"attributes" jsonb DEFAULT '{}'::jsonb NOT NULL,
	"perspective_key" varchar DEFAULT 'world' NOT NULL,
	"source_chunk_id" uuid,
	"promoted_from" uuid
);
--> statement-breakpoint
ALTER TABLE "fact_claim" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
CREATE TABLE "fact_content" (
	"id" uuid PRIMARY KEY NOT NULL,
	"subject_id" uuid NOT NULL,
	"object_id" uuid,
	"predicate" text NOT NULL,
	"statement" text NOT NULL,
	"embedding" halfvec(1024)
);
--> statement-breakpoint
ALTER TABLE "fact_content" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
CREATE TABLE "profile" (
	"embedding" halfvec(1024),
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	"created_by" uuid NOT NULL,
	"scopes" uuid[] DEFAULT '{}'::uuid[] NOT NULL,
	"id" uuid PRIMARY KEY NOT NULL,
	"subject_id" uuid NOT NULL,
	"summary" text NOT NULL,
	CONSTRAINT "uq_profile_scope_subject" UNIQUE("scopes","subject_id")
);
--> statement-breakpoint
ALTER TABLE "profile" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
CREATE TABLE "relation_kind" (
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	"name" text PRIMARY KEY NOT NULL,
	"description" text NOT NULL,
	"domain" text NOT NULL,
	"structural" boolean DEFAULT false NOT NULL
);
--> statement-breakpoint
CREATE TABLE "session_item" (
	"embedding" halfvec(1024),
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	"created_by" uuid NOT NULL,
	"scopes" uuid[] DEFAULT '{}'::uuid[] NOT NULL,
	"id" uuid PRIMARY KEY NOT NULL,
	"kind" varchar NOT NULL,
	"text" text NOT NULL,
	"provenance" jsonb DEFAULT '{}'::jsonb NOT NULL,
	"promoted_at" timestamp with time zone
);
--> statement-breakpoint
ALTER TABLE "session_item" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
CREATE TABLE "watermark" (
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	"created_by" uuid NOT NULL,
	"scopes" uuid[] DEFAULT '{}'::uuid[] NOT NULL,
	"id" uuid PRIMARY KEY NOT NULL,
	"kind" "watermark_kind" NOT NULL,
	"ref" text DEFAULT 'global' NOT NULL,
	"counter" bigint DEFAULT 0 NOT NULL,
	"payload" jsonb DEFAULT '{}'::jsonb NOT NULL,
	CONSTRAINT "uq_watermark_scope_kind_ref" UNIQUE("scopes","kind","ref")
);
--> statement-breakpoint
ALTER TABLE "watermark" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
ALTER TABLE "chunk" ADD CONSTRAINT "chunk_document_id_document_id_fk" FOREIGN KEY ("document_id") REFERENCES "public"."document"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "document" ADD CONSTRAINT "document_promoted_from_document_id_fk" FOREIGN KEY ("promoted_from") REFERENCES "public"."document"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "entity_claim" ADD CONSTRAINT "entity_claim_content_id_entity_content_id_fk" FOREIGN KEY ("content_id") REFERENCES "public"."entity_content"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "entity_content" ADD CONSTRAINT "entity_content_type_entity_kind_name_fk" FOREIGN KEY ("type") REFERENCES "public"."entity_kind"("name") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "fact_claim" ADD CONSTRAINT "fact_claim_content_id_fact_content_id_fk" FOREIGN KEY ("content_id") REFERENCES "public"."fact_content"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "fact_claim" ADD CONSTRAINT "fact_claim_source_chunk_id_chunk_id_fk" FOREIGN KEY ("source_chunk_id") REFERENCES "public"."chunk"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "fact_claim" ADD CONSTRAINT "fact_claim_promoted_from_fact_claim_id_fk" FOREIGN KEY ("promoted_from") REFERENCES "public"."fact_claim"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "fact_content" ADD CONSTRAINT "fact_content_subject_id_entity_content_id_fk" FOREIGN KEY ("subject_id") REFERENCES "public"."entity_content"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "fact_content" ADD CONSTRAINT "fact_content_object_id_entity_content_id_fk" FOREIGN KEY ("object_id") REFERENCES "public"."entity_content"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "fact_content" ADD CONSTRAINT "fact_content_predicate_relation_kind_name_fk" FOREIGN KEY ("predicate") REFERENCES "public"."relation_kind"("name") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "profile" ADD CONSTRAINT "profile_subject_id_entity_content_id_fk" FOREIGN KEY ("subject_id") REFERENCES "public"."entity_content"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "ix_chunk_document_id" ON "chunk" USING btree ("document_id");--> statement-breakpoint
CREATE INDEX "ix_chunk_created_by" ON "chunk" USING btree ("created_by");--> statement-breakpoint
CREATE INDEX "ix_chunk_tsv" ON "chunk" USING gin ("tsv");--> statement-breakpoint
CREATE INDEX "ix_chunk_scopes" ON "chunk" USING gin ("scopes");--> statement-breakpoint
CREATE INDEX "ix_chunk_pending" ON "chunk" USING btree ("id") WHERE processed_at IS NULL;--> statement-breakpoint
CREATE INDEX "ix_chunk_embedding" ON "chunk" USING vchordrq ("embedding" halfvec_cosine_ops);--> statement-breakpoint
CREATE INDEX "ix_community_created_by" ON "community" USING btree ("created_by");--> statement-breakpoint
CREATE INDEX "ix_community_scopes" ON "community" USING gin ("scopes");--> statement-breakpoint
CREATE INDEX "ix_community_embedding" ON "community" USING vchordrq ("embedding" halfvec_cosine_ops);--> statement-breakpoint
CREATE INDEX "ix_document_created_by" ON "document" USING btree ("created_by");--> statement-breakpoint
CREATE INDEX "ix_document_content_hash" ON "document" USING btree ("content_hash");--> statement-breakpoint
CREATE INDEX "ix_document_scopes" ON "document" USING gin ("scopes");--> statement-breakpoint
CREATE INDEX "ix_entity_claim_content_id" ON "entity_claim" USING btree ("content_id");--> statement-breakpoint
CREATE INDEX "ix_entity_claim_created_by" ON "entity_claim" USING btree ("created_by");--> statement-breakpoint
CREATE INDEX "ix_entity_claim_scopes" ON "entity_claim" USING gin ("scopes");--> statement-breakpoint
CREATE INDEX "ix_entity_content_embedding" ON "entity_content" USING vchordrq ("embedding" halfvec_cosine_ops);--> statement-breakpoint
CREATE INDEX "ix_entity_content_name_lower" ON "entity_content" USING btree (lower(name));--> statement-breakpoint
CREATE INDEX "ix_entity_content_name_trgm" ON "entity_content" USING gin (lower(name) gin_trgm_ops);--> statement-breakpoint
CREATE INDEX "ix_fact_claim_content_id" ON "fact_claim" USING btree ("content_id");--> statement-breakpoint
CREATE INDEX "ix_fact_claim_created_by" ON "fact_claim" USING btree ("created_by");--> statement-breakpoint
CREATE INDEX "ix_fact_claim_perspective_key" ON "fact_claim" USING btree ("perspective_key");--> statement-breakpoint
CREATE INDEX "ix_fact_claim_source_chunk_id" ON "fact_claim" USING btree ("source_chunk_id");--> statement-breakpoint
CREATE INDEX "ix_fact_claim_promoted_from" ON "fact_claim" USING btree ("promoted_from");--> statement-breakpoint
CREATE INDEX "ix_fact_claim_scopes" ON "fact_claim" USING gin ("scopes");--> statement-breakpoint
CREATE INDEX "ix_fact_claim_valid" ON "fact_claim" USING gist ("valid");--> statement-breakpoint
CREATE INDEX "ix_fact_claim_recorded" ON "fact_claim" USING gist ("recorded");--> statement-breakpoint
CREATE INDEX "ix_fact_claim_live" ON "fact_claim" USING gist ("valid") WHERE upper_inf(recorded);--> statement-breakpoint
CREATE UNIQUE INDEX "uq_fact_claim_live" ON "fact_claim" USING btree ("content_id","scopes","perspective_key") WHERE upper_inf(recorded);--> statement-breakpoint
CREATE INDEX "ix_fact_content_subject_id" ON "fact_content" USING btree ("subject_id");--> statement-breakpoint
CREATE INDEX "ix_fact_content_object_id" ON "fact_content" USING btree ("object_id");--> statement-breakpoint
CREATE INDEX "ix_fact_content_embedding" ON "fact_content" USING vchordrq ("embedding" halfvec_cosine_ops);--> statement-breakpoint
CREATE INDEX "ix_profile_subject_id" ON "profile" USING btree ("subject_id");--> statement-breakpoint
CREATE INDEX "ix_profile_created_by" ON "profile" USING btree ("created_by");--> statement-breakpoint
CREATE INDEX "ix_profile_scopes" ON "profile" USING gin ("scopes");--> statement-breakpoint
CREATE INDEX "ix_profile_embedding" ON "profile" USING vchordrq ("embedding" halfvec_cosine_ops);--> statement-breakpoint
CREATE INDEX "ix_session_item_created_by" ON "session_item" USING btree ("created_by");--> statement-breakpoint
CREATE INDEX "ix_session_item_promoted_at" ON "session_item" USING btree ("promoted_at");--> statement-breakpoint
CREATE INDEX "ix_session_item_scopes" ON "session_item" USING gin ("scopes");--> statement-breakpoint
CREATE INDEX "ix_session_item_embedding" ON "session_item" USING vchordrq ("embedding" halfvec_cosine_ops);--> statement-breakpoint
CREATE INDEX "ix_watermark_created_by" ON "watermark" USING btree ("created_by");--> statement-breakpoint
CREATE INDEX "ix_watermark_scopes" ON "watermark" USING gin ("scopes");--> statement-breakpoint
CREATE VIEW "public"."live_fact" WITH (security_barrier = true, security_invoker = true) AS (select "fact_claim"."id", "fact_claim"."content_id", "fact_claim"."created_by", "fact_claim"."scopes", "fact_claim"."valid", "fact_claim"."recorded", "fact_claim"."last_accessed", "fact_claim"."access_count", "fact_claim"."attributes", "fact_claim"."perspective_key", "fact_claim"."source_chunk_id", "fact_claim"."promoted_from", "fact_content"."subject_id", "fact_content"."object_id", "fact_content"."predicate", "fact_content"."statement", "fact_content"."embedding" from "fact_claim" inner join "fact_content" on "fact_content"."id" = "fact_claim"."content_id" where upper_inf("fact_claim"."recorded") AND ("fact_claim"."valid" IS NULL OR "fact_claim"."valid" @> now()));--> statement-breakpoint
CREATE POLICY "scope_read" ON "chunk" AS PERMISSIVE FOR SELECT TO "aizk_app" USING (document_id IN (SELECT id FROM document));--> statement-breakpoint
CREATE POLICY "scope_insert" ON "chunk" AS PERMISSIVE FOR INSERT TO "aizk_app" WITH CHECK (cardinality("chunk"."scopes") > 0 AND "chunk"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'write')));--> statement-breakpoint
CREATE POLICY "scope_update" ON "chunk" AS PERMISSIVE FOR UPDATE TO "aizk_app" USING (cardinality("chunk"."scopes") > 0 AND "chunk"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'write'))) WITH CHECK (cardinality("chunk"."scopes") > 0 AND "chunk"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'write')));--> statement-breakpoint
CREATE POLICY "scope_delete" ON "chunk" AS PERMISSIVE FOR DELETE TO "aizk_app" USING (cardinality("chunk"."scopes") > 0 AND "chunk"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'write')));--> statement-breakpoint
CREATE POLICY "scope_read" ON "community" AS PERMISSIVE FOR SELECT TO "aizk_app" USING (cardinality("community"."scopes") > 0 AND ("community"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'read'))
			OR (cardinality("community"."scopes") = 1 AND "community"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'public')))));--> statement-breakpoint
CREATE POLICY "scope_insert" ON "community" AS PERMISSIVE FOR INSERT TO "aizk_app" WITH CHECK (cardinality("community"."scopes") > 0 AND "community"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'write')));--> statement-breakpoint
CREATE POLICY "scope_delete" ON "community" AS PERMISSIVE FOR DELETE TO "aizk_app" USING (cardinality("community"."scopes") > 0 AND "community"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'write')));--> statement-breakpoint
CREATE POLICY "scope_read" ON "document" AS PERMISSIVE FOR SELECT TO "aizk_app" USING (cardinality("document"."scopes") > 0 AND ("document"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'read'))
			OR (cardinality("document"."scopes") = 1 AND "document"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'public')))));--> statement-breakpoint
CREATE POLICY "scope_insert" ON "document" AS PERMISSIVE FOR INSERT TO "aizk_app" WITH CHECK (cardinality("document"."scopes") > 0 AND "document"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'write')));--> statement-breakpoint
CREATE POLICY "scope_update" ON "document" AS PERMISSIVE FOR UPDATE TO "aizk_app" USING (cardinality("document"."scopes") > 0 AND "document"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'write'))) WITH CHECK (cardinality("document"."scopes") > 0 AND "document"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'write')));--> statement-breakpoint
CREATE POLICY "scope_read" ON "entity_claim" AS PERMISSIVE FOR SELECT TO "aizk_app" USING (cardinality("entity_claim"."scopes") > 0 AND ("entity_claim"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'read'))
			OR (cardinality("entity_claim"."scopes") = 1 AND "entity_claim"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'public')))));--> statement-breakpoint
CREATE POLICY "scope_insert" ON "entity_claim" AS PERMISSIVE FOR INSERT TO "aizk_app" WITH CHECK (cardinality("entity_claim"."scopes") > 0 AND "entity_claim"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'write')));--> statement-breakpoint
CREATE POLICY "content_read" ON "entity_content" AS PERMISSIVE FOR SELECT TO "aizk_app" USING (id IN (SELECT content_id FROM entity_claim));--> statement-breakpoint
CREATE POLICY "content_insert" ON "entity_content" AS PERMISSIVE FOR INSERT TO "aizk_app" WITH CHECK (true);--> statement-breakpoint
CREATE POLICY "scope_read" ON "fact_claim" AS PERMISSIVE FOR SELECT TO "aizk_app" USING (cardinality("fact_claim"."scopes") > 0 AND ("fact_claim"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'read'))
			OR (cardinality("fact_claim"."scopes") = 1 AND "fact_claim"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'public')))));--> statement-breakpoint
CREATE POLICY "scope_insert" ON "fact_claim" AS PERMISSIVE FOR INSERT TO "aizk_app" WITH CHECK (cardinality("fact_claim"."scopes") > 0 AND "fact_claim"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'write')));--> statement-breakpoint
CREATE POLICY "scope_update" ON "fact_claim" AS PERMISSIVE FOR UPDATE TO "aizk_app" USING (cardinality("fact_claim"."scopes") > 0 AND "fact_claim"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'write'))) WITH CHECK (cardinality("fact_claim"."scopes") > 0 AND "fact_claim"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'write')));--> statement-breakpoint
CREATE POLICY "content_read" ON "fact_content" AS PERMISSIVE FOR SELECT TO "aizk_app" USING (id IN (SELECT content_id FROM fact_claim));--> statement-breakpoint
CREATE POLICY "content_insert" ON "fact_content" AS PERMISSIVE FOR INSERT TO "aizk_app" WITH CHECK (true);--> statement-breakpoint
CREATE POLICY "scope_read" ON "profile" AS PERMISSIVE FOR SELECT TO "aizk_app" USING (cardinality("profile"."scopes") > 0 AND ("profile"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'read'))
			OR (cardinality("profile"."scopes") = 1 AND "profile"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'public')))));--> statement-breakpoint
CREATE POLICY "scope_insert" ON "profile" AS PERMISSIVE FOR INSERT TO "aizk_app" WITH CHECK (cardinality("profile"."scopes") > 0 AND "profile"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'write')));--> statement-breakpoint
CREATE POLICY "scope_update" ON "profile" AS PERMISSIVE FOR UPDATE TO "aizk_app" USING (cardinality("profile"."scopes") > 0 AND "profile"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'write'))) WITH CHECK (cardinality("profile"."scopes") > 0 AND "profile"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'write')));--> statement-breakpoint
CREATE POLICY "scope_read" ON "session_item" AS PERMISSIVE FOR SELECT TO "aizk_app" USING (cardinality("session_item"."scopes") > 0 AND ("session_item"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'read'))
			OR (cardinality("session_item"."scopes") = 1 AND "session_item"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'public')))));--> statement-breakpoint
CREATE POLICY "scope_insert" ON "session_item" AS PERMISSIVE FOR INSERT TO "aizk_app" WITH CHECK (cardinality("session_item"."scopes") > 0 AND "session_item"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'write')));--> statement-breakpoint
CREATE POLICY "scope_update" ON "session_item" AS PERMISSIVE FOR UPDATE TO "aizk_app" USING (cardinality("session_item"."scopes") > 0 AND "session_item"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'write'))) WITH CHECK (cardinality("session_item"."scopes") > 0 AND "session_item"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'write')));--> statement-breakpoint
CREATE POLICY "scope_read" ON "watermark" AS PERMISSIVE FOR SELECT TO "aizk_app" USING (cardinality("watermark"."scopes") > 0 AND ("watermark"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'read'))
			OR (cardinality("watermark"."scopes") = 1 AND "watermark"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'public')))));--> statement-breakpoint
CREATE POLICY "scope_insert" ON "watermark" AS PERMISSIVE FOR INSERT TO "aizk_app" WITH CHECK (cardinality("watermark"."scopes") > 0 AND "watermark"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'write')));--> statement-breakpoint
CREATE POLICY "scope_update" ON "watermark" AS PERMISSIVE FOR UPDATE TO "aizk_app" USING (cardinality("watermark"."scopes") > 0 AND "watermark"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'write'))) WITH CHECK (cardinality("watermark"."scopes") > 0 AND "watermark"."scopes" <@ ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text((NULLIF(current_setting('app.scopes', true), ''))::jsonb -> 'write')));