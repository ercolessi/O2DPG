#!/usr/bin/env python3
#
# Basically, this script allows a user to compare
# 1. 2 corresponding ROOT files containing either histograms or QC Monitoring objects
# 2. 2 corresponding simulation directories
#
# The RelVal suite is run with
# o2dpg_release_validation.py rel-val -i <file-or-sim-dir1> -j <file-or-sim-dir2>
#
# If 2 sim directories should be compared, it the files to be compared must be given via a config JSON
# via --dirs-config
# See O2DPG/RelVal/config/rel_val_sim_dirs_default.json for as an example
#
# The full help message would be
# usage: o2dpg_release_validation.py rel-val [-h] -i [INPUT1 ...] -j
#                                            [INPUT2 ...] [--with-test-chi2]
#                                            [--with-test-bincont]
#                                            [--with-test-numentries]
#                                            [--chi2-threshold CHI2_THRESHOLD]
#                                            [--rel-mean-diff-threshold REL_MEAN_DIFF_THRESHOLD]
#                                            [--rel-entries-diff-threshold REL_ENTRIES_DIFF_THRESHOLD]
#                                            [--select-critical]
#                                            [--threshold THRESHOLD]
#                                            [--no-plots]
#                                            [--use-values-as-thresholds USE_VALUES_AS_THRESHOLDS]
#                                            [--dir-config DIR_CONFIG]
#                                            [--dir-config-enable [DIR_CONFIG_ENABLE ...]]
#                                            [--dir-config-disable [DIR_CONFIG_DISABLE ...]]
#                                            [--output OUTPUT]
#
# optional arguments:
#   -h, --help            show this help message and exit
#   -i [INPUT1 ...], --input1 [INPUT1 ...]
#                         EITHER first set of input files for comparison OR
#                         first input directory from simulation for comparison
#   -j [INPUT2 ...], --input2 [INPUT2 ...]
#                         EITHER second set of input files for comparison OR
#                         second input directory from simulation for comparison
#   --with-test-chi2      run chi2 test
#   --with-test-bincont   run bin-content test
#   --with-test-numentries
#                         run number-of-entries test
#   --chi2-threshold CHI2_THRESHOLD
#                         Chi2 threshold
#   --rel-mean-diff-threshold REL_MEAN_DIFF_THRESHOLD
#                         Threshold of relative difference in mean
#   --rel-entries-diff-threshold REL_ENTRIES_DIFF_THRESHOLD
#                         Threshold of relative difference in number of entries
#   --select-critical     Select the critical histograms and dump to file
#   --threshold THRESHOLD
#                         threshold for how far file sizes are allowed to
#                         diverge before warning
#   --no-plots            disable plotting
#   --use-values-as-thresholds USE_VALUES_AS_THRESHOLDS
#                         Use values from another run as thresholds for this one
#   --dir-config DIR_CONFIG
#                         What to take into account in a given directory
#   --dir-config-enable [DIR_CONFIG_ENABLE ...]
#                         only enable these top keys in your dir-config
#   --dir-config-disable [DIR_CONFIG_DISABLE ...]
#                         disable these top keys in your dir-config (precedence
#                         over dir-config-enable)
#   --output OUTPUT, -o OUTPUT
#                         output directory

import sys
import argparse
import re
from os import environ, makedirs
from os.path import join, abspath, exists, isfile, isdir, dirname, relpath
from glob import glob
from subprocess import Popen, PIPE, STDOUT
from pathlib import Path
from itertools import combinations
from shlex import split
import json
import matplotlib.pyplot as plt

# make sure O2DPG + O2 is loaded
O2DPG_ROOT=environ.get('O2DPG_ROOT')

if O2DPG_ROOT is None:
    print('ERROR: This needs O2DPG loaded')
    sys.exit(1)

ROOT_MACRO=join(O2DPG_ROOT, "RelVal", "ReleaseValidation.C")

from ROOT import TFile, gDirectory, gROOT, TChain, TH1D

DETECTORS_OF_INTEREST_HITS = ["ITS", "TOF", "EMC", "TRD", "PHS", "FT0", "HMP", "MFT", "FDD", "FV0", "MCH", "MID", "CPV", "ZDC", "TPC"]

REL_VAL_SEVERITY_MAP = {"GOOD": 0, "WARNING": 1, "NONCRIT_NC": 2, "CRIT_NC": 3, "BAD": 4}
REL_VAL_SEVERITY_COLOR_MAP = {"GOOD": "green", "WARNING": "orange", "NONCRIT_NC": "cornflowerblue", "CRIT_NC": "navy", "BAD": "red"}

gROOT.SetBatch()

def is_sim_dir(path):
    """
    Decide whether or not path points to a simulation directory
    """
    if not isdir(path):
        return False
    if not glob(f"{path}/pipeline*"):
        # assume there must be pipeline_{metrics,action} in there
        return False
    return True


def find_mutual_files(dirs, glob_pattern, *, grep=None):
    """
    Find mutual files recursively in list of dirs

    Args:
        dirs: iterable
            directories to take into account
        glob_pattern: str
            pattern used to apply glob to only seach for some files
        grep: iterable
            additional list of patterns to grep for
    Returns:
        list: intersection of found files
    """
    files = []
    for d in dirs:
        glob_path = f"{d}/**/{glob_pattern}"
        files.append(glob(glob_path, recursive=True))

    for f, d in zip(files, dirs):
        f.sort()
        for i, _ in enumerate(f):
            # strip potential leading /
            f[i] = f[i][len(d):].lstrip("/")

    # build the intersection
    if not files:
        return []

    intersection = files[0]
    for f in files[1:]:
        intersection = list(set(intersection) & set(f))

    # apply additional grepping if patterns are given
    if grep:
        intersection_cache = intersection.copy()
        intersection = []
        for g in grep:
            for ic in intersection_cache:
                if g in ic:
                    intersection.append(ic)

    # Sort for convenience
    intersection.sort()

    return intersection


def exceeding_difference_thresh(sizes, threshold=0.1):
    """
    Find indices in sizes where value exceeds threshold
    """
    diff_indices = []
    for i1, i2 in combinations(range(len(sizes)), 2):
        diff = abs(sizes[i1] - sizes[i2])
        if diff / sizes[i2] > threshold or diff / sizes[i2] > threshold:
            diff_indices.append((i1, i2))
    return diff_indices


def file_sizes(dirs, threshold):
    """
    Compare file sizes of mutual files in given dirs
    """
    intersection = find_mutual_files(dirs, "*.root")

    # prepare for convenient printout
    max_col_lengths = [0] * (len(dirs) + 1)
    sizes = [[] for _ in dirs]

    # extract file sizes
    for f in intersection:
        max_col_lengths[0] = max(max_col_lengths[0], len(f))
        for i, d in enumerate(dirs):
            size = Path(join(d, f)).stat().st_size
            max_col_lengths[i + 1] = max(max_col_lengths[i + 1], len(str(size)))
            sizes[i].append(size)

    # prepare dictionary to be dumped and prepare printout
    collect_dict = {"directories": dirs, "files": {}, "threshold": threshold}
    top_row = "| " + " | ".join(dirs) + " |"
    print(f"\n{top_row}\n")
    for i, f in enumerate(intersection):
        compare_sizes = []
        o = f"{f:<{max_col_lengths[0]}}"
        for j, s in enumerate(sizes):
            o += f" | {str(s[i]):<{max_col_lengths[j+1]}}"
            compare_sizes.append(s[i])
        o = f"| {o} |"

        diff_indices =  exceeding_difference_thresh(compare_sizes, threshold)
        if diff_indices:
            o += f"  <==  EXCEEDING threshold of {threshold} at columns {diff_indices} |"
            collect_dict["files"][f] = compare_sizes
        else:
            o += " OK |"
        print(o)
    return collect_dict


def load_root_file(path, option="READ"):
    """
    Convenience wrapper to open a ROOT file
    """
    f = TFile.Open(path, option)
    if not f or f.IsZombie():
        print(f"WARNING: ROOT file {path} might not exist or could not be opened")
        return None
    return f


def make_generic_histograms_from_log_file(filenames1, filenames2, output_filepath1, output_filepath2, patterns, field_numbers, names):

    values1 = [[] for _ in names]
    values2 = [[] for _ in names]

    for filename in filenames1:
        with open(filename, "r", encoding="utf-8") as f:
            for line in f:
                for i, (pattern, field_number) in enumerate(zip(patterns, field_numbers)):
                    if not re.search(pattern, line):
                        continue
                    values1[i].append(float(line.split()[field_number]))
    for filename in filenames2:
        with open(filename, "r", encoding="utf-8") as f:
            for line in f:
                for i, (pattern, field_number) in enumerate(zip(patterns, field_numbers)):
                    if not re.search(pattern, line):
                        continue
                    values2[i].append(float(line.split()[field_number]))

    file1 = TFile(output_filepath1, "RECREATE")
    for values, name in zip(values1, names):
        h1 = TH1D(name, "", 1, 0, 1)
        h1.Fill(0.5, sum(values))
        h1.Write()
    file1.Close()
    file2 = TFile(output_filepath2, "RECREATE")
    for values, name in zip(values2, names):
        h2 = TH1D(name, "", 1, 0, 1)
        h2.Fill(0.5, sum(values))
        h2.Write()
    file2.Close()


def rel_val_files(files1, files2, args, output_dir):
    """
    RelVal for 2 ROOT files, simply a wrapper around ReleaseValidation.C macro
    """
    if not exists(output_dir):
        makedirs(output_dir)
    select_critical = "kTRUE" if args.select_critical else "kFALSE"
    no_plots = "kTRUE" if args.no_plots else "kFALSE"
    in_thresholds = args.use_values_as_thresholds if args.use_values_as_thresholds else ""
    file1 = [abspath(f) for f in files1]
    file1 = ",".join(file1)
    file2 = [abspath(f) for f in files2]
    file2 = ",".join(file2)
    cmd = f"\\(\\\"{file1}\\\",\\\"{file2}\\\",{args.test},{args.chi2_threshold},{args.rel_mean_diff_threshold},{args.rel_entries_diff_threshold},{select_critical},\\\"{abspath(in_thresholds)}\\\"\\)"
    cmd = f"root -l -b -q {ROOT_MACRO}{cmd}"
    log_file = join(abspath(output_dir), "rel_val.log")
    print(f"==> Running {cmd}\nwith log file at {log_file}")
    p = Popen(split(cmd), cwd=output_dir, stdout=PIPE, stderr=STDOUT, universal_newlines=True)
    log_file = open(log_file, 'w')
    for line in p.stdout:
        log_file.write(line)
        #sys.stdout.write(line)
    p.wait()
    log_file.close()
    return 0

def rel_val_files_only(args):
    return rel_val_files(args.input1, args.input2, args, args.output)

def print_summary(filename, *, summary_only=False):
    """
    Check if any 2 histograms have a given severity level after RelVal
    """

    summary = None
    with open(filename, "r") as f:
        summary = json.load(f)

    test_n_hist_map = {s: [] for s in REL_VAL_SEVERITY_MAP}

    # need to re-arrange the JSON structure abit for per-test result pie charts
    for histo_name, tests in summary.items():
        # loop over tests done
        for test in tests:
            test_name = test["test_name"]
            if test_name != "test_summary":
                continue
            result = test["result"]
            test_n_hist_map[result].append(histo_name)

    n_all = sum(len(v) for v in test_n_hist_map.values())
    print(f"\n#####\nNumber of compared histograms: {n_all} out of which severity is")
    print("Out of those:")
    for sev, histos in test_n_hist_map.items():
        print(f"Severity {sev}: {len(histos)}")
    print("\n#####\n")

    return test_n_hist_map


def rel_val_log_file(dir1, dir2, files, output_dir, args, patterns, field_numbers, names, *, combine_patterns=None):
    """
    RelVal for 2 ROOT files containing a TTree to be compared
    """
    # Prepare file paths for TChain
    to_be_chained1 = []
    to_be_chained2 = []
    output_dirs = []

    # possibly combine common files, for instance when they come from different timeframes
    if combine_patterns:
        for cp in combine_patterns:
            chained1 = [join(dir1, hf) for hf in files if cp in hf]
            chained2 = [join(dir2, hf) for hf in files if cp in hf]
            if not chained1 or not chained2:
                continue
            to_be_chained1.append(chained1)
            to_be_chained2.append(chained2)
            output_dirs.append(f"{cp}_dir")
    else:
        to_be_chained1 = []
        to_be_chained2 = []
        for hf in files:
            to_be_chained1.append(join(dir1, hf))
            to_be_chained2.append(join(dir2, hf))
            output_dirs.append(f"{hf}_dir")

    # paths for chains prepared, output directory names specified, do RelVal
    for tbc1, tbc2, od in zip(to_be_chained1, to_be_chained2, output_dirs):
        output_dir_hf = join(output_dir, od)
        if not exists(output_dir_hf):
            makedirs(output_dir_hf)

        make_generic_histograms_from_log_file(tbc1, tbc2, join(output_dir_hf, "file1.root"), join(output_dir_hf, "file2.root"), patterns, field_numbers, names)
        # after we created files containing histograms, they can be compared with the standard RelVal
        rel_val_files((abspath(join(output_dir_hf, "file1.root")),), (abspath(join(output_dir_hf, "file2.root")),), args, output_dir_hf)
    return 0

def plot_pie_chart_single(summary, out_dir, title):
    test_n_hist_map = {}

    # need to re-arrange the JSON structure abit for per-test result pie charts
    for histo_name, tests in summary.items():
        # loop over tests done
        for test in tests:
            test_name = test["test_name"];
            if test_name not in test_n_hist_map:
                test_n_hist_map[test_name] = {}
            result = test["result"]
            if result not in test_n_hist_map[test_name]:
                test_n_hist_map[test_name][result] = 0
            test_n_hist_map[test_name][result] += 1


    for which_test, flags in test_n_hist_map.items():
        labels = []
        colors = []
        n_histos = []
        for flag, count in flags.items():
            labels.append(flag)
            n_histos.append(count)
            colors.append(REL_VAL_SEVERITY_COLOR_MAP[flag])

        figure, ax = plt.subplots(figsize=(20, 20))
        ax.pie(n_histos, explode=[0.05 for _ in labels], labels=labels, autopct="%1.1f%%", startangle=90, textprops={"fontsize": 30}, colors=colors)
        ax.axis("equal")
        ax.axis("equal")

        figure.suptitle(f"{title} ({which_test})", fontsize=40)
        save_path = join(out_dir, f"pie_chart_{which_test}.png")
        figure.savefig(save_path)
        plt.close(figure)


def extract_from_summary(summary, fields):
    """
    Extract a fields from summary per test and histogram name
    """
    test_histo_value_map = {}
    # need to re-arrange the JSON structure abit for per-test result pie charts
    for histo_name, tests in summary.items():
        # loop over tests done
        for test in tests:
            test_name = test["test_name"];
            if test_name not in test_histo_value_map:
                test_histo_value_map[test_name] = {field: [] for field in fields}
                test_histo_value_map[test_name]["histograms"] = []
            if not test["comparable"]:
                continue
            test_histo_value_map[test_name]["histograms"].append(histo_name)
            for field in fields:
                test_histo_value_map[test_name][field].append(test[field])
    return test_histo_value_map


def plot_values_single(summary, out_dir, title):
    test_histo_value_map = extract_from_summary(summary, ["value", "threshold"])

    for which_test, histos_values_thresolds in test_histo_value_map.items():

        figure, ax = plt.subplots(figsize=(20, 20))
        ax.plot(range(len(histos_values_thresolds["histograms"])), histos_values_thresolds["value"], label="values", marker="x")
        ax.plot(range(len(histos_values_thresolds["histograms"])), histos_values_thresolds["threshold"], label="thresholds", marker="o")
        ax.legend(loc="best", fontsize=20)
        ax.set_xticks(range(len(histos_values_thresolds["histograms"])))
        ax.set_xticklabels(histos_values_thresolds["histograms"], rotation=90)
        ax.tick_params("both", labelsize=20)

        figure.suptitle(f"{title} ({which_test})", fontsize=40)
        save_path = join(out_dir, f"test_values_thresholds_{which_test}.png")
        figure.tight_layout()
        figure.savefig(save_path)
        plt.close(figure)


def plot_compare_summaries(summaries, fields, out_dir, *, labels=None):
    """
    if labels is given, it needs to have the same length as summaries
    """
    test_histo_value_maps = [extract_from_summary(summary, fields) for summary in summaries]

    # need to get intersection of tests
    test_names = list(set().union(*[list(t.keys()) for t in test_histo_value_maps]))

    if not labels:
        labels = [f"summary_{i}" for i, _ in enumerate(summaries)]

    for test_name in test_names:
        histogram_names_intersection = []
        # First we figure out the intersection of histograms ==> histograms in common
        for test_histo_value_map in test_histo_value_maps:
            if test_name not in test_histo_value_map:
                continue
            this_map = test_histo_value_map[test_name]
            if not histogram_names_intersection:
                histogram_names_intersection = this_map["histograms"]
            histogram_names_intersection =  list(set(histogram_names_intersection) & set(this_map["histograms"]))
        values = {field: [[] for _ in test_histo_value_maps] for field in fields}
        # now fill the correct values of the fields for the histograms in common
        for map_index, test_histo_value_map in enumerate(test_histo_value_maps):
            this_map = test_histo_value_map[test_name]
            for histo_name in histogram_names_intersection:
                i = this_map["histograms"].index(histo_name)
                for f in fields:
                    values[f][map_index].append(this_map[f][i])

        # now plot
        figure, ax = plt.subplots(figsize=(20, 20))
        for field, values_lists in values.items():
            for label, single_values in zip(labels, values_lists):
                ax.plot(range(len(histogram_names_intersection)), single_values, label=f"{label}_{field}")
        ax.legend(loc="best", fontsize=20)
        ax.set_xticks(range(len(histogram_names_intersection)))
        ax.set_xticklabels(histogram_names_intersection, rotation=90)
        ax.tick_params("both", labelsize=20)
        save_path = join(out_dir, f"plot_{test_name}_{'_'.join(labels)}.png")
        figure.tight_layout()
        figure.savefig(save_path)
        plt.close(figure)


def plot_additional_summary(in_dir):
    """
    Make a summary per histogram (that should be able to be parsed by Grafana eventually)
    """
    print("==> Plotting <==")
    file_paths = glob(f"{in_dir}/**/Summary.json", recursive=True)
    summary = []

    for path in file_paths:
        # go through all we found
        current_summary = None
        with open(path, "r") as f:
            current_summary = json.load(f)
        # remove the file name, used as the top key for this collection
        rel_val_path = "/".join(path.split("/")[:-1])
        title = relpath(rel_val_path, in_dir)
        plot_pie_chart_single(current_summary, rel_val_path, title)
        plot_values_single(current_summary, rel_val_path, title)


def make_summary(in_dir):
    """
    Make a summary per histogram (that should be able to be parsed by Grafana eventually)
    """
    print("==> Make summary <==")
    file_paths = glob(f"{in_dir}/**/Summary.json", recursive=True)
    summary = {}

    for path in file_paths:
        # go through all we found
        current_summary = None
        with open(path, "r") as f:
            current_summary = json.load(f)
        # remove the file name, used as the top key for this collection
        rel_val_path = "/".join(path.split("/")[:-1])
        type_specific = relpath(rel_val_path, in_dir)
        rel_path_plot = join(type_specific, "overlayPlots")
        type_global = type_specific.split("/")[0]
        make_summary = {}
        for histo_name, tests in current_summary.items():
            summary[histo_name] = tests
            # loop over tests done
            for test in tests:
                test["name"] = histo_name
                test["type_global"] = type_global
                test["type_specific"] = type_specific
                test["rel_path_plot"] = join(rel_path_plot, f"{histo_name}.png")
    return summary


def rel_val_histograms(dir1, dir2, files, output_dir, args):
    """
    Simply another wrapper to combine multiple files where we expect them to contain histograms already
    """
    for f in files:
        output_dir_f = join(output_dir, f"{f}_dir")
        if not exists(output_dir_f):
            makedirs(output_dir_f)
        rel_val_files((join(dir1, f),), (join(dir2, f),), args, output_dir_f)


def rel_val_sim_dirs(args):
    """
    Make full RelVal for 2 simulation directories
    """
    dir1 = args.input1[0]
    dir2 = args.input2[0]
    output_dir = args.output

    look_for = "Summary.json"
    summary_dict = {}

    # file sizes, this is just done on everything to be found in both directories
    file_sizes_to_json = file_sizes([dir1, dir2], 0.5)
    with open(join(output_dir, "file_sizes.json"), "w") as f:
        json.dump(file_sizes_to_json, f, indent=2)


    config = args.dir_config
    with open(config, "r") as f:
        config = json.load(f)

    run_over_keys = list(config.keys())
    if args.dir_config_enable:
        run_over_keys = [rok for rok in run_over_keys if rok in args.dir_config_enable]
    if args.dir_config_disable:
        run_over_keys = [rok for rok in run_over_keys if rok not in args.dir-dir_config_disable]
    if not run_over_keys:
        print("WARNING: All keys in config disabled, nothing to do")
        return 0

    for rok in run_over_keys:
        current_dir_config = config[rok]
        # now run over name and path (to glob)
        for name, path in current_dir_config.items():
            current_files = find_mutual_files((dir1, dir2), path)
            if not current_files:
                print(f"WARNING: Nothing found for search path {path}, continue")
                continue
            in1 = [join(dir1, cf) for cf in current_files]
            in2 = [join(dir2, cf) for cf in current_files]
            current_output_dir = join(output_dir, rok, name)
            if not exists(current_output_dir):
                makedirs(current_output_dir)
            rel_val_files(in1, in2, args, current_output_dir)
    return 0

def make_new_threshold_file(json_path, out_filepath):
    json_in = None
    with open(json_path, "r") as f:
        json_in = json.load(f)
    with open(out_filepath, "w") as f:
        for histo_name, tests in json_in.items():
            for t in tests:
                print(t["result"])
                if not t["comparable"]:
                    continue
                f.write(f"{histo_name},{t['test_name']},{t['value']}\n")


def rel_val(args):
    """
    Entry point for RelVal
    """
    func = None
    # construct the bit mask
    args.test = 1 * args.with_test_chi2 + 2 * args.with_test_bincont + 4 * args.with_test_numentries
    if not args.test:
        args.test = 7
    if not exists(args.output):
        makedirs(args.output)
    if args.use_values_as_thresholds:
        out_path = make_new_threshold_file(args.use_values_as_thresholds, join(args.output, "use_thresholds.dat"))
        args.use_values_as_thresholds = join(args.output, "use_thresholds.dat")
    if is_sim_dir(args.input1[0]) and is_sim_dir(args.input2[0]):
        if not args.dir_config:
            print("ERROR: RelVal to be run on 2 directories. Please provide a configuration what to validate.")
            return 1
        func = rel_val_sim_dirs
    else:
        func = rel_val_files_only
        for f in args.input1 + args.input2:
            if not isfile(f):
                func = None
                break
        # simply check if files, assume that they would be ROOT files in that case
    if not func:
        print("Please provide either 2 sets of files or 2 simulation directories as input.")
        return 1
    if not exists(args.output):
        makedirs(args.output)
    func(args)
    with open(join(args.output, "SummaryGlobal.json"), "w") as f:
        json.dump(make_summary(args.output), f, indent=2)
    plot_additional_summary(args.output)
    print_summary(join(args.output, "SummaryGlobal.json"), summary_only=True)
    return 0


def inspect(args):
    """
    Inspect a Summary.json in view of RelVal severity
    """
    path = args.path

    def get_filepath(d):
        summary_global = join(path, "SummaryGlobal.json")
        if exists(summary_global):
            return summary_global
        summary = join(path, "Summary.json")
        if exists(summary):
            return summary
        print(f"Can neither find {summary_global} nor {summary}. Nothing to work with.")
        return None

    if isdir(path):
        path = get_filepath(path)
        if not path:
            return 1

    print_summary(path)

    return 0


def compare(args):
    """
    Compare 2 RelVal outputs with one another
    """
    output_dir = args.output

    if not args.difference and not args.compare_values:
        args.difference, args.compare_values = (True, True)

    # plot comparison of values and thresholds of both RelVals per test
    if args.compare_values:
        summaries_common = find_mutual_files((args.input[0], args.input[1]), "Summary.json")
        for summaries in summaries_common:
            output_dir_this = join(output_dir, f"{summaries.replace('/', '_')}_dir")
            if not exists(output_dir_this):
                makedirs(output_dir_this)
            summaries = [join(input, summaries) for input in args.input]
            for i, _ in enumerate(summaries):
                with open(summaries[i], "r") as f:
                    summaries[i] = json.load(f)
            plot_compare_summaries(summaries, ["threshold", "value"], output_dir_this)

    # print the histogram names with different severities per test
    if args.difference:
        summaries = [join(input, "SummaryGlobal.json") for input in args.input]
        for i, summary in enumerate(summaries):
            if not exists(summary):
                print(f"WARNING: Cannot find expected {summary}.")
                return 1

        s = "\nCOMPARING RELVAL SUMMARY\n"
        summaries = [print_summary(summary) for summary in summaries]
        print("Histograms with different RelVal results from 2 RelVal runs")
        for severity in REL_VAL_SEVERITY_MAP:
            intersection = list(set(summaries[0][severity]) & set(summaries[1][severity]))
            s += f"==> SEVERITY {severity} <=="
            print(f"==> SEVERITY {severity} <==")
            s += "\n"
            for i, summary in enumerate(summaries):
                print(f"FILE {i+1}")
                s += f"FILE {i+1}: "
                counter = 0
                for histo_name in summary[severity]:
                    if histo_name not in intersection:
                        print(f"  {histo_name}")
                        counter += 1
                s += f"{counter}   "
            s += "\n"
        print(s)
    return 0


def influx(args):
    """
    Create an influxDB metrics file
    """
    output_dir = args.dir
    json_in = join(output_dir, "SummaryGlobal.json")
    if not exists(json_in):
        print(f"Cannot find expected JSON summary {json_in}.")
        return 1
    table_name = "O2DPG_MC_ReleaseValidation"
    if args.table_suffix:
        table_name = f"{table_name}_{args.table_suffix}"
    tags_out = ""
    if args.tags:
        for t in args.tags:
            t_split = t.split("=")
            if len(t_split) != 2 or not t_split[0] or not t_split[1]:
                print(f"ERROR: Invalid format of tags {t} for InfluxDB")
                return 1
            # we take it apart and put it back together again to make sure there are no whitespaces etc
            tags_out += f",{t_split[0].strip()}={t_split[1].strip()}"

    # always the same
    row_tags = table_name + tags_out

    out_file = join(output_dir, "influxDB.dat")

    summary = None
    with open(json_in, "r") as f:
        summary = json.load(f)
    with open(out_file, "w") as f:
        for i, (histo_name, tests) in enumerate(summary.items()):
            if not tests:
                continue
            s = f"{row_tags},type_global={tests[0]['type_global']},type_specific={tests[0]['type_specific']},id={i}"
            if args.web_storage:
                s += f",web_storage={join(args.web_storage, tests[0]['rel_path_plot'])}"
            s += f" histogram_name=\"{histo_name}\""
            for test in tests:
                s += f",{test['test_name']}={REL_VAL_SEVERITY_MAP[test['result']]}"
            f.write(f"{s}\n")


def main():
    """entry point when run directly from command line"""
    parser = argparse.ArgumentParser(description='Wrapping ReleaseValidation macro')

    common_file_parser = argparse.ArgumentParser(add_help=False)
    common_file_parser.add_argument("-i", "--input1", nargs="*", help="EITHER first set of input files for comparison OR first input directory from simulation for comparison", required=True)
    common_file_parser.add_argument("-j", "--input2", nargs="*", help="EITHER second set of input files for comparison OR second input directory from simulation for comparison", required=True)

    sub_parsers = parser.add_subparsers(dest="command")
    rel_val_parser = sub_parsers.add_parser("rel-val", parents=[common_file_parser])
    rel_val_parser.add_argument("--with-test-chi2", dest="with_test_chi2", action="store_true", help="run chi2 test")
    rel_val_parser.add_argument("--with-test-bincont", dest="with_test_bincont", action="store_true", help="run bin-content test")
    rel_val_parser.add_argument("--with-test-numentries", dest="with_test_numentries", action="store_true", help="run number-of-entries test")
    rel_val_parser.add_argument("--chi2-threshold", dest="chi2_threshold", type=float, help="Chi2 threshold", default=1.5)
    rel_val_parser.add_argument("--rel-mean-diff-threshold", dest="rel_mean_diff_threshold", type=float, help="Threshold of relative difference in mean", default=1.5)
    rel_val_parser.add_argument("--rel-entries-diff-threshold", dest="rel_entries_diff_threshold", type=float, help="Threshold of relative difference in number of entries", default=0.01)
    rel_val_parser.add_argument("--select-critical", dest="select_critical", action="store_true", help="Select the critical histograms and dump to file")
    rel_val_parser.add_argument("--threshold", type=float, default=0.1, help="threshold for how far file sizes are allowed to diverge before warning")
    rel_val_parser.add_argument("--no-plots", dest="no_plots", action="store_true", help="disable plotting")
    rel_val_parser.add_argument("--use-values-as-thresholds", dest="use_values_as_thresholds", help="Use values from another run as thresholds for this one")
    rel_val_parser.add_argument("--dir-config", dest="dir_config", help="What to take into account in a given directory")
    rel_val_parser.add_argument("--dir-config-enable", dest="dir_config_enable", nargs="*", help="only enable these top keys in your dir-config")
    rel_val_parser.add_argument("--dir-config-disable", dest="dir_config_disable", nargs="*", help="disable these top keys in your dir-config (precedence over dir-config-enable)")

    rel_val_parser.add_argument("--output", "-o", help="output directory", default="rel_val")
    rel_val_parser.set_defaults(func=rel_val)

    inspect_parser = sub_parsers.add_parser("inspect")
    inspect_parser.add_argument("path", help="either complete file path to a Summary.json or SummaryGlobal.json or directory where one of the former is expected to be")
    inspect_parser.set_defaults(func=inspect)

    compare_parser = sub_parsers.add_parser("compare", parents=[common_file_parser])
    compare_parser.add_argument("--output", "-o", help="output directory", default="rel_val_comparison")
    compare_parser.add_argument("--difference", action="store_true", help="plot histograms with different severity")
    compare_parser.add_argument("--compare-values", action="store_true", help="plot value and threshold comparisons of RelVals")
    compare_parser.set_defaults(func=compare)

    influx_parser = sub_parsers.add_parser("influx")
    influx_parser.add_argument("--dir", help="directory where ReleaseValidation was run", required=True)
    influx_parser.add_argument("--web-storage", dest="web_storage", help="full base URL where the RelVal results are supposed to be")
    influx_parser.add_argument("--tags", nargs="*", help="tags to be added for influx, list of key=value")
    influx_parser.add_argument("--table-suffix", dest="table_suffix", help="prefix for table name")
    influx_parser.set_defaults(func=influx)

    args = parser.parse_args()
    return(args.func(args))

if __name__ == "__main__":
    sys.exit(main())
