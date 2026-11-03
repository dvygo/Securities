#pragma once
// premarket/args.hpp -- the option parser behind every command.
//
// Deliberately stricter than argparse, which 7.x used: options are matched by their full
// name only (argparse accepted any unambiguous prefix, so --dat meant --date-dir and a new
// option could silently change what an old cron line meant); a non-repeatable option given
// twice is an error rather than "the last one wins"; a flag given a value (--dry-run=1) is
// an error; --name=value and --name value are the same. Every error is a UsageError,
// which the CLI turns into exit status 2.
