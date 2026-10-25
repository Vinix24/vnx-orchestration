- `vnx deliverable retarget <id> --objective <track> --reason "..."` (track `deliverable-retarget-withdraw`): a proposed or
  ready deliverable moves to another track with one `deliverable_retargeted` event, instead of a `close --evidence` that
  attests a PR which never delivered the work. A ready one goes back to proposed; started or ended ones are refused.
