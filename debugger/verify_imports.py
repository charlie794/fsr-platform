#!/usr/bin/env python3
"""
Verification script to test if all project files can import correctly.
Run this after applying the fixes to verify everything works together.
"""

import sys
import os

# Add current directory to path if needed
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print("=" * 70)
print("PROJECT IMPORT VERIFICATION")
print("=" * 70)

# Test results
passed = []
failed = []

def test_import(module_name, class_or_func=None):
    """Test importing a module and optionally a class/function from it."""
    try:
        mod = __import__(module_name)
        
        if class_or_func:
            if not hasattr(mod, class_or_func):
                raise AttributeError(f"Module {module_name} has no attribute {class_or_func}")
            obj = getattr(mod, class_or_func)
            result = f"✅ {module_name}.{class_or_func}"
        else:
            result = f"✅ {module_name}"
        
        passed.append(result)
        print(result)
        return True
    except Exception as e:
        result = f"❌ {module_name}" + (f".{class_or_func}" if class_or_func else "")
        error = f"   Error: {type(e).__name__}: {e}"
        failed.append(result + "\n" + error)
        print(result)
        print(error)
        return False


print("\n1. TESTING HARDWARE ADAPTERS")
print("-" * 70)
test_import("duet_adapter", "DuetAdapter")
test_import("daq_adapter", "DaqAdapter")
test_import("smac_adapter", "SmacAdapter")

print("\n2. TESTING DOMAIN/DATA MODELS")
print("-" * 70)
test_import("models", "TestStep")
test_import("models", "RunConfig")
test_import("models", "store")

print("\n3. TESTING RUNNERS")
print("-" * 70)
test_import("test_runner", "TestRunnerWorker")
test_import("grid_runner", "GridRunner")

print("\n4. TESTING DATA I/O")
print("-" * 70)
test_import("plan_loader", "load_grid_plan_csv")
test_import("writers", "make_writers")
test_import("file_dialogs", "save_prompt")

print("\n5. TESTING PROCESSING")
print("-" * 70)
test_import("criteria_eval")
test_import("criteria_loader")
test_import("filters")

print("\n6. TESTING UTILITIES")
print("-" * 70)
test_import("path_utils", "resolve_criteria_path")
test_import("calibration")

print("\n7. TESTING UI COMPONENTS")
print("-" * 70)
test_import("mode_dialog", "ModeDialog")
test_import("operator_mode", "OperatorModePopup")
test_import("engineering_mode", "EngineeringMode")
test_import("job_details", "get_job_details")
test_import("resistance_calibration", "ResistanceCalibration")
test_import("force_calibration", "ForceCalibration")

print("\n8. TESTING MAIN ENTRY POINT")
print("-" * 70)
test_import("main", "main")

print("\n9. TESTING OPTIONAL/DEBUG TOOLS")
print("-" * 70)
test_import("home", "run_home_sequence")
test_import("equations_debugger")
test_import("soft_touch_debug")

print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"✅ Passed: {len(passed)}")
print(f"❌ Failed: {len(failed)}")

if failed:
    print("\n⚠️  FAILURES:")
    for failure in failed:
        print(failure)
    print("\n❌ Some imports failed. Review errors above.")
    sys.exit(1)
else:
    print("\n🎉 All imports successful! Project files are synchronized.")
    sys.exit(0)
