// The recall program ported from the Python server's retrieval/query.py: one RLS-filtered
// SQL statement built by one function, reading top to bottom in retrieval order. Hybrid
// source search fuses dense and lexical chunk rankings; dense fact seeds blend access
// recency into distance; one-hop neighbors and, on relationship routes, a personalized
// PageRank expansion widen them; auxiliary lanes join by route; every lane competes for one
// token budget walked recursively in the database; and the kept facts get their access
// accounting bumped in the same statement. Row-for-row identical to the Python build.
import { sql, type SQL } from 'drizzle-orm';

import { settings } from '../settings';

export type Route = 'local' | 'global' | 'multihop';

export interface RecallParams {
	vector: string;
	text: string;
	mentions: string[];
	k: number;
	budget: number;
}

// postgres-js serializes a JS array parameter as a record, so the mentions bind as one
// Postgres array literal string cast back to text[].
const textArray = (values: string[]): string =>
	`{${values.map((value) => `"${value.replaceAll('\\', '\\\\').replaceAll('"', '\\"')}"`).join(',')}}`;

export const buildRecallStatement = (route: Route, params: RecallParams, packed = true): SQL => {
	const s = settings;
	const n = (value: number): SQL => sql.raw(value.toFixed(0));
	// JS numbers erase the int/float distinction (String(4.0) is '4'), which once turned
	// Postgres float division into integer division; floats always render a decimal point.
	const f = (value: number): SQL => {
		const literal = Number.isInteger(value) ? `${value}.0` : `${value}`;
		return sql.raw(literal);
	};
	const vec = sql`${params.vector}::halfvec`;
	const floor = f(s.recallMaxDistance);
	const visible = sql`upper_inf(cl.recorded) and (cl.valid is null or cl.valid @> now())`;

	// Lane identity: the packing bitmask position is fixed; priority follows the route.
	const bits: Record<string, number> = {
		profile: 1,
		overview: 2,
		communities: 4,
		facts: 8,
		working_memory: 16,
		sources: 32
	};
	const order =
		route === 'global'
			? ['overview', 'communities', 'facts', 'sources', 'profile', 'working_memory']
			: ['facts', 'sources', 'working_memory', 'profile', 'overview', 'communities'];
	const lane = (name: string): SQL => {
		const projection = `'${name}' as lane, ${order.indexOf(name)} as priority, ${bits[name]} as lane_bit`;
		return sql.raw(projection);
	};
	const noProvenance = sql`null::uuid as fact_id, null::uuid as source_chunk_id,
		null::text as source_title, null::text as source_uri`;

	const lexical =
		s.bm25Backend === 'tsvector'
			? sql`
	lexical_ranked as (
		select c.id, c.document_id, ts_rank(c.tsv, plainto_tsquery('english', ${params.text})) as raw_rank
		from chunk c
		where c.tsv @@ plainto_tsquery('english', ${params.text})
		order by raw_rank desc limit ${n(s.fusionDepth)}
	),
	lexical_chunk as (
		select id, document_id, row_number() over (order by raw_rank desc) as rank from lexical_ranked
	)`
			: sql`
	lexical_ranked as (
		select c.id, c.document_id,
			c.bm25 <&> to_bm25query('ix_chunk_bm25', tokenize(${params.text}, 'aizk_bm25')) as raw_rank
		from chunk c
		order by raw_rank limit ${n(s.fusionDepth)}
	),
	lexical_chunk as (
		select id, document_id, row_number() over (order by raw_rank) as rank
		from lexical_ranked where raw_rank < 0
	)`;

	// Personalized PageRank, only on relationship routes: query-named entities seed decisive
	// mass (exact at full mention mass, trigram matches similarity-scaled), dense seeds are
	// the fallback, and mass diffuses one bounded degree-normalized hop at a time. A fact
	// then scores by its weaker endpoint's mass, so a semantically distant hop outranks the
	// near-duplicates that merely touch one popular entity.
	const hop = (index: number): SQL => {
		const previous = index === 1 ? 'seed_mass' : `hop_${index - 1}`;
		return sql`
	frontier_${n(index)} as (
		select entity_id, mass from ${sql.raw(previous)}
		order by mass desc limit ${n(s.graphPprFrontier)}
	),
	edge_${n(index)} as (
		select fc.subject_id as src, fc.object_id as dst
		from frontier_${n(index)} f
		join fact_content fc on fc.subject_id = f.entity_id
		join fact_claim cl on cl.content_id = fc.id
		where ${visible} and fc.object_id is not null
		union all
		select fc.object_id, fc.subject_id
		from frontier_${n(index)} f
		join fact_content fc on fc.object_id = f.entity_id
		join fact_claim cl on cl.content_id = fc.id
		where ${visible}
	),
	hop_${n(index)} as (
		select e.dst as entity_id, sum(f.mass * ${f(s.graphPprDamping)} / greatest(d.edges, 1)) as mass
		from edge_${n(index)} e
		join frontier_${n(index)} f on f.entity_id = e.src
		join (select src, count(*) as edges from edge_${n(index)} group by src) d on d.src = e.src
		group by e.dst
	)`;
	};
	const hops = Array.from({ length: s.multihopMaxHops }, (_, i) => hop(i + 1));
	const spreads = [
		sql`select entity_id, mass from seed_mass`,
		...hops.map((_, i) => {
			const table = `hop_${i + 1}`;
			return sql.raw(`select entity_id, mass from ` + table);
		})
	];
	const graph =
		route === 'multihop'
			? sql`
	mention_entity as (
		select ec.id as entity_id, cast(${f(s.graphMentionMass)} as double precision) as mass
		from entity_content ec
		where lower(ec.name) = any(${textArray(params.mentions)}::text[])
		${
			s.graphMentionFuzzy
				? sql`union all
		select ec.id, ${f(s.graphMentionMass)} * similarity(lower(ec.name), mention.mention)
		from unnest(${textArray(params.mentions)}::text[]) as mention(mention)
		join entity_content ec on lower(ec.name) % mention.mention
		where lower(ec.name) != mention.mention`
				: sql``
		}
	),
	seed_mass as (
		select entity_id, sum(mass) as mass from (
			select entity_id, mass from mention_entity
			union all
			select * from (
				select ec.id, ${f(s.graphEntitySeedWeight)} / (1.0 + (ec.embedding <=> ${vec})) as mass
				from entity_content ec where ec.embedding is not null
				order by ec.embedding <=> ${vec} limit ${n(s.graphSeedEntities)}
			) dense_entities where not exists (select 1 from mention_entity)
			union all
			select * from (
				select subject_id, ${f(s.graphFactSeedWeight)} / (1.0 + distance) from dense_fact
				union all
				select object_id, ${f(s.graphFactSeedWeight)} / (1.0 + distance) from dense_fact
				where object_id is not null
			) fact_endpoints where not exists (select 1 from mention_entity)
		) seeded group by entity_id
	),
	${sql.join(hops, sql`,`)},
	entity_mass as (
		select entity_id, sum(mass) as mass from (${sql.join(spreads, sql` union all `)}) spread
		group by entity_id order by sum(mass) desc limit ${n(s.graphMassWindow)}
	),
	multihop_fact as (
		select cl.id, -least(sm.mass, coalesce(om.mass, sm.mass * ${f(s.graphDanglingFactor)})) as ordering
		from fact_content fc
		join fact_claim cl on cl.content_id = fc.id
		join entity_mass sm on sm.entity_id = fc.subject_id
		left join entity_mass om on om.entity_id = fc.object_id
		where ${visible} and fc.embedding is not null
		order by ordering limit ${n(s.graphFactsK)}
	),`
			: sql``;

	// Fact parts merge by per-part rank so graph-only evidence is never suppressed by raw
	// cosine distance; the lanes selected into the packer follow route and settings.
	const factParts = [
		sql`select id, blended as ordering from dense_fact`,
		sql`select id, ordering from neighbor_fact`,
		...(route === 'multihop' ? [sql`select id, ordering from multihop_fact`] : [])
	].map(
		(part) => sql`select id, row_number() over (order by ordering) as part_rank from (${part}) part`
	);

	const lanes = [
		sql`select * from fact_lane`,
		sql`select * from source_lane`,
		...(s.sessionRecallK > 0 ? [sql`select * from working_memory_lane`] : []),
		...(s.profiles ? [sql`select * from profile_lane`] : []),
		...(route === 'global'
			? [sql`select * from communities_lane`, sql`select * from overview_lane`]
			: [])
	];

	const program = sql`
	with recursive
	dense_fact_content as materialized (
		select fc.id, fc.subject_id, fc.object_id, fc.embedding <=> ${vec} as distance
		from fact_content fc
		where fc.embedding is not null and (fc.embedding <=> ${vec}) < ${floor}
		order by distance limit ${n(s.fusionDepth)}
	),
	dense_fact as (
		select cl.id, dfc.subject_id, dfc.object_id, dfc.distance,
			dfc.distance
				- ${f(s.recallRecencyWeight)} * power(0.5,
					(extract(epoch from now() - coalesce(cl.last_accessed, lower(cl.recorded))) / 86400.0)
					/ ${f(s.recallRecencyHalfLifeDays)})
				- ${f(s.recallFrequencyWeight)} * ln(1 + cl.access_count) as blended
		from fact_claim cl
		join dense_fact_content dfc on dfc.id = cl.content_id
		where ${visible}
		order by blended limit ${params.k}
	),
	seed_entity as (
		select subject_id as entity_id from dense_fact
		union
		select object_id from dense_fact where object_id is not null
	),
	neighbor_touch as (
		select cl.id, fc.embedding <=> ${vec} as ordering
		from seed_entity se
		join fact_content fc on fc.subject_id = se.entity_id
		join fact_claim cl on cl.content_id = fc.id
		where ${visible} and fc.embedding is not null and cl.id not in (select id from dense_fact)
		union
		select cl.id, fc.embedding <=> ${vec}
		from seed_entity se
		join fact_content fc on fc.object_id = se.entity_id
		join fact_claim cl on cl.content_id = fc.id
		where ${visible} and fc.embedding is not null and cl.id not in (select id from dense_fact)
	),
	neighbor_fact as (select id, ordering from neighbor_touch order by ordering limit ${params.k}),
	${graph}
	fact_candidate as (
		select id, min(part_rank) as rank from (${sql.join(factParts, sql` union all `)}) fact_parts
		group by id order by min(part_rank), id
		limit ${params.k} * ${n(s.factCandidateFactor)}
	),
	fact_lane as (
		select ${lane('facts')}, cl.id as evidence_id, cast(fcand.rank as double precision) as ordering,
			concat('- ',
				case when coalesce(cl.attributes ->> 'epistemic_kind', 'world') = 'world'
						and cl.attributes ->> 'speaker_label' is null then ''
					else concat('[', coalesce(cl.attributes ->> 'speaker_label', 'unknown speaker'),
						case when cl.attributes ->> 'speaker_role' is not null
							then concat(', ', cl.attributes ->> 'speaker_role') else '' end,
						', ', coalesce(cl.attributes ->> 'epistemic_kind', 'world'), '] ') end,
				'(', fc.predicate, ') ', fc.statement) as line,
			cl.id as fact_id, cl.source_chunk_id, fd.title as source_title, fd.source_uri, cl.created_by
		from fact_candidate fcand
		join fact_claim cl on cl.id = fcand.id
		join fact_content fc on fc.id = cl.content_id
		left join chunk fs on fs.id = cl.source_chunk_id
		left join document fd on fd.id = fs.document_id
		where ${visible}
	),
	dense_ranked as (
		select c.id, c.document_id, c.embedding <=> ${vec} as distance
		from chunk c
		where c.embedding is not null and (c.embedding <=> ${vec}) < ${floor}
		order by distance limit ${n(s.fusionDepth)}
	),
	dense_chunk as (
		select id, document_id, row_number() over (order by distance) as rank from dense_ranked
	),
	${lexical},
	fused_chunk as (
		select id, document_id, sum(1.0 / (${n(s.rrfK)} + rank)) as rrf_score
		from (select * from dense_chunk union all select * from lexical_chunk) chunk_lanes
		group by id, document_id
	),
	chunk_scored as (
		select f.id, f.document_id, d.title as document_title, d.source_uri, c.text, c.created_by,
			c.provenance ->> 'speaker_label' as speaker_label,
			c.provenance ->> 'speaker_role' as speaker_role,
			f.rrf_score + case when d.promoted_from is not null then ${f(s.promotedBonus)} else 0.0 end as score,
			row_number() over (
				partition by f.document_id
				order by f.rrf_score + case when d.promoted_from is not null then ${f(s.promotedBonus)} else 0.0 end desc
			) as document_rank
		from fused_chunk f
		join document d on d.id = f.document_id
		join chunk c on c.id = f.id
	),
	chunk_capped as (
		select * from chunk_scored where document_rank <= ${n(s.recallPerDocument)}
		order by score desc limit ${params.k}
	),
	source_lane as (
		select ${lane('sources')}, h.id as evidence_id, cast(-h.score as double precision) as ordering,
			concat('[', round(h.score::numeric, 3)::text, '] ',
				coalesce(h.document_title, h.source_uri, 'untitled'),
				case when h.speaker_label is not null
					then concat(' by ', h.speaker_label,
						case when h.speaker_role is not null then concat(' (', h.speaker_role, ')') else '' end)
					else '' end,
				e'\n  ', left(regexp_replace(h.text, '\\s+', ' ', 'g'), ${n(s.snippetChars)})) as line,
			null::uuid as fact_id, h.id as source_chunk_id, h.document_title as source_title,
			h.source_uri, h.created_by
		from chunk_capped h
	),
	working_memory_lane as (
		select ${lane('working_memory')}, i.id as evidence_id,
			cast(i.embedding <=> ${vec} as double precision) as ordering,
			concat('- [', i.kind, '] ',
				case when i.provenance ->> 'speaker_label' is not null
					then concat(i.provenance ->> 'speaker_label', ': ') else '' end,
				i.text) as line,
			${noProvenance}, i.created_by
		from session_item i
		where i.embedding is not null and i.promoted_at is null and (i.embedding <=> ${vec}) < ${floor}
		order by i.embedding <=> ${vec} limit ${n(s.sessionRecallK)}
	),
	profile_lane as (
		select ${lane('profile')}, p.id as evidence_id,
			cast(p.embedding <=> ${vec} as double precision) as ordering, p.summary as line,
			${noProvenance}, p.created_by
		from profile p
		where p.embedding is not null and (p.embedding <=> ${vec}) < ${floor}
		order by p.embedding <=> ${vec} limit ${n(s.profileRecallK)}
	),
	communities_lane as (
		select ${lane('communities')}, co.id as evidence_id,
			cast(co.embedding <=> ${vec} as double precision) as ordering,
			concat('- ', co.label, ': ', co.summary) as line,
			${noProvenance}, co.created_by
		from community co
		where co.embedding is not null and (co.embedding <=> ${vec}) < ${floor}
		order by co.embedding <=> ${vec} limit ${n(s.communityRecallK)}
	),
	overview_lane as (
		select ${lane('overview')}, ec.id as evidence_id,
			cast(ec.embedding <=> ${vec} as double precision) as ordering,
			concat('- L', cast(cl.attributes ->> 'level' as integer), ' ', ec.name, ': ',
				cl.attributes ->> 'summary') as line,
			${noProvenance}, cl.created_by
		from entity_content ec
		join entity_claim cl on cl.content_id = ec.id
		where ec.type = 'raptor_summary'
			and cast(cl.attributes ->> 'level' as integer) = (
				select max(cast(icl.attributes ->> 'level' as integer))
				from entity_claim icl
				join entity_content iec on iec.id = icl.content_id
				where iec.type = 'raptor_summary' and cast(icl.attributes ->> 'level' as integer) >= 1
			)
			and ec.embedding is not null
		order by ec.embedding <=> ${vec} limit ${n(s.raptorK)}
	),
	context_candidate as (${sql.join(lanes, sql` union all `)}),
	ordered_context as materialized (
		select *,
			cast(row_number() over (order by priority, ordering, evidence_id) as integer) as position,
			cast(ceil(char_length(line) / ${f(s.recallCharsPerToken)}) as integer) as line_tokens,
			cast(ceil(char_length(lane || ':') / ${f(s.recallCharsPerToken)}) as integer) as header_tokens
		from context_candidate
	)`;
	// The rerank path stops here: the cross-encoder reorders the raw candidates between two
	// round trips and the packer twin replays the identical budget walk in the server.
	if (!packed) return sql`${program} select * from ordered_context order by position`;
	return sql`${program},
	packed_context as (
		select 0 as position, 0 as used_tokens, 0 as opened_lanes, false as is_kept,
			null::text as lane, null::text as line, null::uuid as fact_id,
			null::uuid as source_chunk_id, null::text as source_title, null::text as source_uri,
			null::uuid as created_by
		union all
		select o.position,
			case when p.used_tokens + o.line_tokens
					+ case when p.opened_lanes & o.lane_bit = 0 then o.header_tokens else 0 end + 1 <= ${params.budget}
				then p.used_tokens + o.line_tokens
					+ case when p.opened_lanes & o.lane_bit = 0 then o.header_tokens else 0 end + 1
				else p.used_tokens end,
			case when p.used_tokens + o.line_tokens
					+ case when p.opened_lanes & o.lane_bit = 0 then o.header_tokens else 0 end + 1 <= ${params.budget}
				then p.opened_lanes | o.lane_bit else p.opened_lanes end,
			p.used_tokens + o.line_tokens
				+ case when p.opened_lanes & o.lane_bit = 0 then o.header_tokens else 0 end + 1 <= ${params.budget},
			o.lane, o.line, o.fact_id, o.source_chunk_id, o.source_title, o.source_uri, o.created_by
		from packed_context p
		join ordered_context o on o.position = p.position + 1
	),
	record_selected_access as (
		update fact_claim set last_accessed = now(), access_count = fact_claim.access_count + 1
		where upper_inf(fact_claim.recorded)
			and fact_claim.id in (select fact_id from packed_context where is_kept and fact_id is not null)
		returning fact_claim.id
	)
	select lane, line, source_chunk_id, source_title, source_uri, created_by, used_tokens
	from packed_context where is_kept order by position`;
};
