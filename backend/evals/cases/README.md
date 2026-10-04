# Case files for a real drawing set

`real_set_qa.json`, `real_set_count.json` and `real_set_search.json` were written for one real 25-sheet set (a
tenant-improvement drawing set uploaded during development; the PDF itself is not in the
repo). Each expected value was checked by hand against the drawings. They show the case
format documented in `../run_eval.py`; for your own drawings, copy and edit them.

Counting cases that expect `null` are objects the set does not contain (a correct system
says "not counted"). `cnt-toilet` is a case the engine cannot count from the PDF's data
(there is no toilet legend swatch or tag), so the honest outcomes are "not counted" or a
flagged vision-model estimate, never "cross-checked".
