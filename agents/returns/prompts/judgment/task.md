---
prompt_id: judgment_task
version: 1.0.0
summary: Fixed per-inspection task instruction, placed before the return photos.
---
TASK: Inspect the returned unit shown in the RETURN PHOTOS that follow. For every photo fill photo_reports.
Decide unit presence, identity against the PRODUCT CARD, completeness for every component of the parts list,
and condition against the CONDITION RUBRIC, following the rules of the system instruction. Report
uncertainty with reasons and retake requests. Return only the JSON object.
