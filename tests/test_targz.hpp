#pragma once
// premarket/targz.hpp -- the backups under data/_state/backups/ are ustar archives
// compressed with zlib, written by premarket itself (no tar binary on the host is needed
// to take one). They must be ordinary archives: the test lists and extracts one with the
// system tar, which is how an operator restores a backup. Directories recurse in sorted
// order; a name longer than 100 bytes is split into the ustar prefix and name fields, and
// one that cannot be split is refused rather than truncated.
