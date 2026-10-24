# Visual review and local revisions

Use one authored project JSON and one narration plan as the edit sources. Keep scene, object and narration segment IDs stable. `timeline.json`, glyphs, audio mixes and frames are derived outputs. An initial composition script may create the first source; do not rerun it over user edits with old embedded wording.

## Before exporting video

Provide each scene's full static composition with its title, exact labels, principal subjects/layout and matching narration. Review readability and object placement together; select scene count for the story. `build.py --sheet` automatically creates `build/review/` with this review package. To generate it separately:

```text
python <skill>/scripts/review_project.py --project <work>/project.json --audio-plan <work>/narration.json --compiled <work>/build --output-dir <work>/review
```

The compiled argument must match the authored visuals; stale previews are refused. Without `--compiled`, the helper makes a silent visual-only build and captures new scene previews without synthesizing speech. `review.html` contains embedded snapshots and works offline; `review.md` and `revision-index.json` provide a readable draft and precise scene/object mapping. Optional scene `narrationText` records an excerpt when one continuous recording spans several scenes; the full recording is edited once.

The HTML lets the user edit on-board and narration text and download a patch. Preview images show the current source, not an unsaved live edit: apply the patch and regenerate the review to see the new composition. Object positions, dimensions and colors can be updated through agent-authored patches or the source JSON. This is a review form, not a drag-and-drop animation editor.

Show the review before a requested planning/approval round. If the user already approved the design or explicitly requested a direct remake/export, create the review files and continue; an extra confirmation is not required by this skill.

## Apply a reviewed change

Translate descriptions such as “second scene, orange label” to stable IDs. Users need not find code or frame numbers.

```text
python <skill>/scripts/revise_project.py --project <work>/project.json --audio-plan <work>/narration.json --patch <work>/revision.patch.json
```

Minimal visual patch:

```json
{
  "schemaVersion": "handdrawn-explainer/patch/1",
  "baseProjectSha256": "COPY_FROM_REVISION_INDEX",
  "operations": [
    {"target":"visual","sceneId":"capture","elementId":"co2-label","set":{"text":"二氧化碳","x":1040,"size":30}}
  ]
}
```

Use the actual IDs and hash in the current index. `set` updates allowed text, geometry, style or timing fields without replacing the entire object or changing its ID. Unknown IDs, unsupported fields and stale project hashes are rejected before writing. Nested transform/region values merge with existing fields. The helper writes a revision report describing affected outputs and preserves untouched content. `--output` may write an alternate authored JSON beside the original so asset paths remain valid.

Speech operations use `target:"speech"`, a `segmentId`, an overlapping `sceneId` and `baseAudioPlanSha256` from the review. Changing text also clears an obsolete spokenText pronunciation override unless a replacement is supplied. When a source audioFile has explicit generated/TTS provenance, changing its spoken content invalidates that file binding. For an imported recording with uncertain origin, provide a different matching audioFile or `regenerate:true`; never silently reuse the old recording for new words. Regeneration uses an available configured speech adapter, not an automatic paid service.

If speech changes, measured phrase markers and corresponding scene excerpts become stale. The helper clears them and marks `narrationTimingStatus:"needs-realignment"`. Generate/import the changed recording, measure it, update scene excerpts and phrase/drawing timings, then clear the status with the new verified timing. Do not export using stale phrase alignment. A single continuous recording may require re-synthesizing that whole short recording; a sentence-clip project can reuse untouched clips.

## Rebuild consistently

| Change | Refresh | Reuse |
|---|---|---|
| On-board words, size, position | Changed glyphs, layout, affected action timing, review and video | Illustrations and unchanged voice sources |
| Object geometry/color | Its paths/style, layout, tool sound, review and video | Unchanged art and narration |
| One spoken sentence | Matching audio/cache binding, timing, captions, mix, affected downstream beats | Unchanged illustrations and other voice clips |

Export the complete timeline from the updated sources. Full re-rendering uses cached media; it does not recreate every image or voice clip. This keeps voice, tool position, transitions and captions consistent and avoids accumulating video cut-and-paste fixes. Keep the source, current review, portable audio and validation reports with delivery.
