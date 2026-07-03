from datetime import datetime

import jinja2
from patos import FrozenModel

from ...config import settings
from .community_note import CommunityNote
from .fact_hit import FactHit
from .hit import Hit
from .raptor_note import RaptorNote
from .session_note import SessionNote

# renders a recall bundle as compact text the agent reads, the broad view, the asserted knowledge,
# the fresh working memory, and the supporting passages in widening focus. Blank-line separated
# sections appear only when their lane produced something, so an empty lane leaves no trace.
_TEMPLATE = jinja2.Template(
    """\
{%- if not hits and not facts and not communities and not raptor and not session and not profile %}
no memory recalled for {{ query_repr }}
{%- else -%}
{% if profile %}profile:
{{ profile }}

{% endif -%}
{% if raptor %}overview:
{% for note in raptor %}- L{{ note.level }} {{ note.label }}: {{ note.summary }}
{% endfor %}

{% endif -%}
{% if communities %}communities:
{% for note in communities %}- {{ note.label }}: {{ note.summary }}
{% endfor %}

{% endif -%}
{% if facts %}facts:
{% for fact in facts %}- ({{ fact.predicate }}) {{ fact.statement }}
{% endfor %}

{% endif -%}
{% if session %}working memory:
{% for note in session %}- [{{ note.kind }}] {{ note.text }}
{% endfor %}

{% endif -%}
{% if hits %}sources:
{% for hit in hits %}[{{ "%.3f" | format(hit.score) }}] {{ hit.title }}
  {{ hit.snippet }}
{% endfor %}
{% endif -%}
{%- endif %}""",
    trim_blocks=True,
    lstrip_blocks=True,
)


class RecallResult(FrozenModel):
    """The single fused context a recall returns, the agent's one retrieval surface.

    The agent calls recall and reads this, never deciding the chunk-versus-graph mix itself.

    query: the natural-language query this context answers.
    hits: fused, reranked chunk and fact evidence, best first.
    facts: the matching latest facts, their one-hop neighbors, then the pagerank-reached facts.
    communities: global community summaries for a thematic query, empty for a pointed one.
    raptor: the recursive RAPTOR summaries, the root level for a thematic query and the leaf
        summary level for a pointed one, empty until a tree is built or when the lane is off.
    session: the still-working session items the query matched, the fast front of memory whose
        knowledge has not yet reached the graph, empty when the working lane is off.
    profile: the static-plus-dynamic profile of the top matched entity, null unless profiles on.
    as_of: world-time the graph was read at, the live graph when null.
    """

    query: str
    hits: list[Hit]
    facts: list[FactHit]
    communities: list[CommunityNote]
    raptor: list[RaptorNote]
    session: list[SessionNote] = []
    profile: str | None = None
    as_of: datetime | None

    def render(self) -> str:
        """Render this bundle as compact text the agent reads, facts then chunk snippets."""
        hits = [
            {
                "score": hit.score,
                "title": hit.document_title or hit.source_uri or "untitled",
                "snippet": " ".join(hit.text.split())[: settings.snippet_chars],
            }
            for hit in self.hits
        ]
        return _TEMPLATE.render(
            query_repr=repr(self.query),
            profile=self.profile,
            raptor=self.raptor,
            communities=self.communities,
            facts=self.facts,
            session=self.session,
            hits=hits,
        ).strip()
