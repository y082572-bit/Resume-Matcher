# Explicit Provenance Stage P6-B2a: Deterministic Proposal DOCX Renderer

> **P6-B2A-R1 update**: this document originally described the P6-B2a
> baseline, where the renderer was wired into `build_current_docx_proposal`
> with `header_lines=None` and no production composition root existed. See
> [P6-B2A-R1: production wiring and owner-bound header
> remediation](#p6-b2a-r1-production-wiring-and-owner-bound-header-remediation)
> below for what changed.

## Purpose

P6-B2a ships the first real implementation of the `DocxTemplateProvider`
and `CvDocxRenderingAdapter` Protocols P6-A already defined
(`cv_document_adapters.py`) and wires them into the *existing* P6-A
orchestration (`build_current_docx_proposal`) and the *existing* P6-A
`CvDocumentArtifactRepository` current-proposal slot. It introduces no
second proposal table, no second current-proposal slot, and no new
orchestration function -- `build_current_docx_proposal` already accepted an
injected renderer/template provider and needed no modification.

P6-B2a produces DOCX bytes only. It never generates PDF, never
materializes a working copy, never implements `FinalDocxSourceReader`, and
never touches an API or a frontend -- all of that is P6-B2b/P6-B3/P6-C, per
the architecture gate report this stage implements exactly the first half
of (`GO TO P6-B2A DETERMINISTIC PROPOSAL DOCX IMPLEMENTATION`).

## Visual contract (`cv_docx_visual_contract.py`)

`CvDocxVisualContract` is a closed, frozen, versioned Pydantic model
(`visual_contract_version`, `renderer_version`, `template_version` plus
page geometry, typography, spacing, and structural layout policy).
`body_font_name`/`body_font_size_pt` are pinned via `Literal["Calibri"]`/
`Literal[10]` -- never a caller-tunable default -- satisfying the
non-negotiable "Calibri 10 for body" requirement at the type level, not
just by convention. `page_break_before_section` defaults to
`CvSection.EXPERIENCE`. `compute_visual_contract_fingerprint` covers every
field, so a version-only bump (no other field changed) still changes the
fingerprint -- see `_apply_fixed_core_properties` in `cv_docx_template.py`,
which embeds every version string into a fixed `docProps/core.xml`
`category` field so the bump is also reflected in the actual bytes, not
only in an out-of-band fingerprint.

## Template strategy: T2, no checked-in binary

`cv_docx_template.py`'s `DeterministicDocxTemplateProvider` implements
`DocxTemplateProvider`. `build_base_document` constructs a blank Word
document purely in code from `CvDocxVisualContract` -- page size/margins
and five named paragraph styles (`Normal`, `CvName`, `CvContactLine`,
`CvSectionHeader`, `CvBullet`) -- and pins every volatile core property
(`created`, `modified`, `author`, `revision`, ...) to a fixed constant. No
`.docx` binary asset is ever checked into the repository or read from
disk; `template_fingerprint` on the returned `DocxTemplateHandle` is the
exact raw SHA-256 of `template_bytes` (required by
`build_current_docx_proposal`'s own `pre_render_hash`/`post_render_hash`
verification), not an abstract contract-level fingerprint.

## Deterministic packaging (`cv_docx_canonical_packaging.py`)

`canonicalize_docx_package` is a pure, ZIP-level function applied to every
document this stage saves (both the blank template and the final rendered
proposal): fixed per-entry timestamps (`1980-01-01`), a fixed entry order
(`[Content_Types].xml` first, then lexicographic), a fixed
`external_attr`/`create_system`, and a fixed compression policy
(`ZIP_DEFLATED`, level 6). It never rewrites part *content*, only ZIP
container metadata. Verified empirically (not just by design) across
repeated in-process renders, across `PYTHONHASHSEED`-randomized subprocess
invocations, and via `zipfile` introspection of entry timestamps/order --
see `tests/unit/test_cv_docx_canonical_packaging.py` and
`tests/unit/test_cv_docx_renderer_determinism.py`.

## Render plan (`cv_docx_render_plan.py` / `cv_docx_render_plan_builder.py`)

`CvDocxRenderPlan` is a closed intermediate representation between
approved content and DOCX bytes (Renderer Input variant B from the
architecture gate). `build_cv_docx_render_plan` walks
`ApprovedCvContentResult.draft.sections` in their existing order, emitting
exactly one content-bearing block per `ApprovedCvContentElement`
(`source_element_fingerprint` traces it back 1:1), one `SECTION_HEADER`
block per section, and one explicit `PAGE_BREAK` block immediately before
whichever section `visual_contract.page_break_before_section` names. It
never reorders, drops, or invents an element, never calls a provider, and
is fully synchronous/pure.

## Renderer (`cv_docx_renderer.py`)

`DeterministicCvDocxRenderer` implements `CvDocxRenderingAdapter`. As of
P6-B2A-R1 (R3) it is constructed with an already-complete `CvDocxRenderPlan`
(`DeterministicCvDocxRenderer(visual_contract, render_plan=plan)`) and its
`render()` method only *executes* that plan: it opens the template bytes it
is handed, writes the plan's blocks into it using the template's own
pre-defined named styles (it never redefines a style), and re-serializes
through `canonicalize_docx_package`. Bullet items use a literal bullet
glyph + tab (`"•\t"`) as the marker -- deliberately simpler than the
native-Word-list-numbering approach originally sketched in the architecture
gate, chosen because it is trivially correct and testable while still
making a hyphen-before-bullet structurally impossible (the renderer never
emits `"-"` as a marker). It never calls an LLM, never paraphrases, never
changes a number, never builds a render plan itself, never fetches header
data, never opens a database session, and rejects (fail-closed,
`DocxRenderingFailureCode.CONTENT_INCOMPATIBLE`) any `approved_content_result`
that is not `READY`, or whose `content_plan_fingerprint` does not match the
supplied `content_plan`, or whose fingerprints don't match the render plan
it was constructed with.

### Header wiring is production, as of P6-B2A-R1

See [P6-B2A-R1](#p6-b2a-r1-production-wiring-and-owner-bound-header-remediation)
below: the production composition root
(`cv_docx_proposal_orchestration.py`) resolves an owner-bound
`CvDocxHeaderBinding` and builds a complete render plan with a real
`CvDocxHeaderLines` before ever constructing the renderer, so
`NAME_HEADER`/`CONTACT_LINE` blocks are emitted in production. The renderer
itself still never fabricates identity text it was not given -- it remains
possible to construct it with a header-less render plan (`header_lines=None`
at `build_cv_docx_render_plan` time), exercised directly by
`tests/unit/test_cv_docx_render_plan_builder.py`.

## Determinism, empirically verified

Two renders of the same fixture in the same process, and two renders in
freshly spawned subprocesses with an unset (OS-random) `PYTHONHASHSEED`,
produce byte-identical DOCX output and an identical SHA-256. A
version-only bump of `visual_contract_version`/`renderer_version`/
`template_version` (no other field changed) changes both
`template_fingerprint` and the final rendered `output_sha256`. A change to
any single approved-content element's `content_text` changes the final
bytes and hash; nothing else does.

## What was *not* built at the P6-B2a baseline (superseded by P6-B2A-R1)

At the original P6-B2a baseline, `build_current_docx_proposal` already
accepted an injected `DocxTemplateProvider`/`CvDocxRenderingAdapter` pair,
so no orchestration wrapper existed yet and the renderer always called
`build_cv_docx_render_plan(..., header_lines=None)` internally -- no
`NAME_HEADER`/`CONTACT_LINE` block was ever emitted in production. P6-B2A-R1
(below) replaces that gap with real owner-bound header wiring.

## P6-B2A-R1: production wiring and owner-bound header remediation

P6-B2A-R1 adds the single supported production composition path from an
`ApprovedContentDocumentInput` to a current Proposal DOCX, and closes the
"no header in production" gap the P6-B2a baseline disclosed above.

### R3: composition/rendering separation

`DeterministicCvDocxRenderer` (`cv_docx_renderer.py`) no longer builds a
render plan itself and no longer defaults to `header_lines=None`. Its
constructor now requires an already-complete `CvDocxRenderPlan`
(`DeterministicCvDocxRenderer(visual_contract, render_plan=plan)`); `render()`
only executes that plan (template application, canonical packaging, exact
bytes) after verifying the plan's `approved_cv_content_fingerprint`/
`content_plan_fingerprint` match the caller-supplied
`approved_content_result`/`content_plan`. The renderer never fetches header
data, never opens a database session, never calls
`CandidateIdentityHeaderSqlService`, and never accepts an arbitrary header
string -- composition (owner-bound header resolution, render-plan
construction) is now exclusively the responsibility of the caller.

### Production composition root (`cv_docx_proposal_orchestration.py`)

`generate_current_deterministic_docx_proposal` is the single supported
production path:

    ApprovedContentDocumentInput
    -> JobArtifactOwnerKey revalidation (validate_approved_content_document_input)
    -> CandidateIdentityHeaderSqlService.resolve_header_binding
    -> owner-bound CvDocxHeaderBinding (RESOLVED + owner-key-fingerprint/person match required)
    -> CvDocxHeaderLines (full_name + email/phone/location/linkedin contact tokens; never website)
    -> complete CvDocxRenderPlan (build_cv_docx_render_plan)
    -> DeterministicDocxTemplateProvider + DeterministicCvDocxRenderer(render_plan=...)
    -> existing build_current_docx_proposal
    -> existing CvDocumentArtifactRepository current-proposal CAS slot.

The caller passes only `ApprovedContentDocumentInput`, `CvDocxVisualContract`,
a `CandidateIdentityHeaderSqlService`, a `CvDocumentArtifactRepository`,
`rendering_policy_version`, and `expected_previous_revision` -- never a
header string, `CvDocxHeaderLines`, an arbitrary render plan, a template
provider, a renderer adapter, or template bytes. There is no manual
full_name/email/phone/location/LinkedIn passthrough anywhere on this path:
every header field originates only from `JobArtifactOwnerKey` ->
`candidate_identity_bindings` -> the Master Resume's `PersonalInfo`,
resolved fresh on every call. `website`/`github`/`title` are never read
(`CvDocxHeaderBinding` has no field for them) and a LinkedIn URL containing
the `marekgrabowski` slug is rendered exactly as resolved -- no blanket
substring filter is ever applied.

The result is a closed `DeterministicDocxProposalGenerationResult`
(`GENERATED`/`INPUT_INVALID`/`HEADER_RESOLUTION_FAILED`/
`HEADER_OWNER_MISMATCH`/`RENDER_PLAN_FAILED`/`PROPOSAL_BUILD_FAILED`/
`STORAGE_UNAVAILABLE`) -- never a raw SQLAlchemy/Pydantic/ZIP/python-docx
exception and never a raw `ProposalBuildStatus`/`CvDocxHeaderBindingStatus`.

### Header identity in the fingerprint chain

`cv_document_proposal_builder.py`'s `compute_generation_input_fingerprint`
gained two new, purely additive, optional fields --
`source_personal_info_fingerprint` and `render_plan_fingerprint` (both
`None` for any caller that does not generate a header, preserving prior
P6-A semantics exactly). `build_current_docx_proposal` gained matching
optional keyword parameters, threaded straight through to the fingerprint
computation; no new stored field was added to `CvDocxProposalArtifact`.
This closes the identity chain: a change to
full_name/email/phone/location/LinkedIn changes
`source_personal_info_fingerprint` (`cv_docx_header_binding_builder.py`,
unmodified) -> changes `render_plan_fingerprint` (blocks differ) -> changes
the rendered DOCX bytes -> changes `generated_docx_content_hash` -> changes
`generation_input_fingerprint` -> changes `artifact_fingerprint`. A
`website`-only change never enters this chain at any step (excluded by
`compute_source_personal_info_fingerprint` and by `CvDocxHeaderBinding`
having no `website` field at all), so it never changes
`generation_input_fingerprint` or `artifact_fingerprint`.
