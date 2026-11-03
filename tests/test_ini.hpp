#pragma once
// premarket/ini.hpp -- the INI dialect every 8.0.0 file uses (config.ini, keys.ini, sinks).
//
// Deliberately small and strict, because a config that is misread quietly is worse than one
// that is refused loudly:
//   - [section] headers; section names are case-sensitive ("venue.XNAS")
//   - key = value; keys are letters, digits, '_', '-', '.'; case-sensitive
//   - '#' or ';' starts a comment at the start of a line, or after whitespace inside a value
//     ("enabled = 1  # on" is 1; "dir =   # empty" is empty). A value that must start with
//     '#' or ';', or hold " #", is written in double quotes, where \" and \\ are the only
//     escapes
//   - no interpolation, no continuation lines, no [DEFAULT] -- a value is exactly what is
//     written after '='
//   - a repeated section or key, a key outside a section, or a line without '=' is an error
//     naming the file and line
//
// Typed reads carry the location into every error ("conf/config.ini:12: [paths] data_dir must
// be an absolute path"): integer() is a whole number, flag() is exactly 0 or 1 (no
// true/false/yes/no spellings, so a grep for "enabled = 1" finds every live venue), and
// absolute_path() refuses anything relative. only() rejects keys the schema does not know,
// which is what turns a typo into an error instead of an ignored setting.
