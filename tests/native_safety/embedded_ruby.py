"""Run an isolated Ruby script with the installed DLL, without starting SketchUp.

This process has no SketchUp API, no UI, and does not start the repair listener.
Only the supplied synthetic test adapter provides Sketchup constants.
"""
from __future__ import annotations

import ctypes as C
import json
import os
from pathlib import Path
import sys


def main():
    install = Path(os.environ.get("DWG_AUDIT_RUBY_HOME", "C:/Program Files/SketchUp/SketchUp 2021"))
    candidates = sorted(install.glob("*ruby*.dll"))
    if not candidates:
        print(json.dumps({"environment_unavailable":"No standalone Ruby DLL found"}))
        return 77
    dll_path = candidates[0]
    os.environ["PATH"] = str(install) + os.pathsep + str(install / "Tools/RubyStdLib/platform_specific") + os.pathsep + os.environ.get("PATH", "")
    if os.name == "nt":
        C.windll.kernel32.SetDllDirectoryW(str(install))
    dll_dir = os.add_dll_directory(str(install)) if os.name == "nt" else None
    ruby = C.CDLL(str(dll_path))
    ruby.ruby_sysinit.argtypes = [C.POINTER(C.c_int), C.POINTER(C.POINTER(C.c_char_p))]
    ruby.ruby_sysinit.restype = None
    ruby.ruby_init_stack.argtypes = [C.c_void_p]
    ruby.ruby_init_stack.restype = None
    ruby.ruby_setup.argtypes = []
    ruby.ruby_setup.restype = C.c_int
    ruby.rb_eval_string_protect.argtypes = [C.c_char_p, C.POINTER(C.c_int)]
    ruby.rb_eval_string_protect.restype = C.c_size_t
    ruby.rb_obj_as_string.argtypes = [C.c_size_t]
    ruby.rb_obj_as_string.restype = C.c_size_t
    ruby.rb_string_value_cstr.argtypes = [C.POINTER(C.c_size_t)]
    ruby.rb_string_value_cstr.restype = C.c_char_p
    ruby.rb_errinfo.argtypes = []
    ruby.rb_errinfo.restype = C.c_size_t
    ruby.ruby_cleanup.argtypes = [C.c_int]
    ruby.ruby_cleanup.restype = C.c_int
    argc = C.c_int(1)
    args = (C.c_char_p * 2)(b"isolated-ruby-audit", None)
    argv = C.cast(args, C.POINTER(C.c_char_p))
    ruby.ruby_sysinit(C.byref(argc), C.byref(argv))
    stack_low, stack_high = C.c_size_t(), C.c_size_t()
    C.windll.kernel32.GetCurrentThreadStackLimits(C.byref(stack_low), C.byref(stack_high))
    ruby.ruby_init_stack(C.c_void_p(stack_high.value - 16))
    if ruby.ruby_setup() != 0:
        print(json.dumps({"environment_unavailable":"ruby_setup failed"}))
        return 77
    script = Path(sys.argv[1]).resolve()
    stdlib = install / "Tools/RubyStdLib"
    prefix = "$LOAD_PATH.unshift(" + ",".join(json.dumps(str(p).replace("\\","/"))
                      for p in [stdlib, stdlib / "platform_specific"]) + ");\n"
    code = prefix + script.read_text(encoding="utf-8")
    state = C.c_int()
    result = ruby.rb_eval_string_protect(code.encode("utf-8"), C.byref(state))
    if state.value:
        value = C.c_size_t(ruby.rb_obj_as_string(ruby.rb_errinfo()))
        message = ruby.rb_string_value_cstr(C.byref(value)).decode("utf-8", errors="replace")
        print(json.dumps({"ruby_error":message, "state":state.value}))
        ruby.ruby_cleanup(1)
        return 1
    value = C.c_size_t(ruby.rb_obj_as_string(result))
    output = ruby.rb_string_value_cstr(C.byref(value)).decode("utf-8", errors="replace")
    print(output, flush=True)
    # ruby_cleanup attempts to tear down process-owned extension state; isolate
    # it here, never in the pytest host or an application process.
    ruby.ruby_cleanup(0)
    if dll_dir:
        dll_dir.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


