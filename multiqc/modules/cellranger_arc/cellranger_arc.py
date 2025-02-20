import json
import logging
import re
from typing import Dict, Optional

from multiqc.base_module import BaseMultiqcModule, ModuleNoSamplesFound
from multiqc.modules.cellranger_arc.utils import (
    extract_plot_data,
    subset_header,
    table_data_and_headers,
    set_hidden_cols,
)
from multiqc.plots import linegraph, table

log = logging.getLogger(__name__)


class MultiqcModule(BaseMultiqcModule):
    """
    The module summarizes the main information from Cell Ranger ARC which is useful for QC:

    - sequencing metrics
    - cell metrics
    - targeting metrics
    - mapping metrics
    - atac TSS plots
    - atac insert size distribution plots
    - gex saturaiton plots
    - gex genes per cell plots

    Note that information such as clustering and differential expression are not reported.

    The input files are web summaries generated by Cell Ranger ARC. Expected file names are `*web_summary.html`.
    Sample IDs are parsed directly from the reports.

    If present in the original report, any warning is reported as well.
    """

    def __init__(self):
        super(MultiqcModule, self).__init__(
            name="Cell Ranger ARC",
            anchor="cellranger-arc",
            href="https://www.10xgenomics.com/support/software/cell-ranger-arc/latest",
            info="Analyzes Single Cell Multiome ATAC + Gene Expression data produced by 10X Genomics.",
            doi="10.1038/ncomms14049",
        )
        data_by_sample: Dict[str, Dict] = dict()
        warnings_by_sample: Dict[str, Dict] = dict()
        self.warnings_headers: Dict[str, Dict] = dict()
        self.all_headers: Dict[str, Dict] = dict()
        self.plots_data_by_sample: Dict[str, Dict] = {
            "tss": dict(),
            "insert_size": dict(),
            "saturation": dict(),
            "genes": dict(),
        }

        for f in self.find_log_files("cellranger_arc", filehandles=True):
            summary: Optional[Dict] = None
            for line in f["f"]:
                line = line.strip()
                if line.startswith("const data"):
                    line = line.replace("const data = ", "")
                    summary = json.loads(line)
                    break
            if summary is None:
                continue

            s_name = self.clean_s_name(summary["sample"]["id"], f)
            parsed_data, warnings, plots_data_by_id = self.parse_html_summary(summary)

            # Extract software version
            try:
                version_pair = summary["joint_pipeline_info_table"]["rows"][2]
                # print(version_pair)
                assert version_pair[0] == "Pipeline version"
                version_match = re.search(r"cellranger-arc-([\d\.]+)", version_pair[1])
                if version_match:
                    self.add_software_version(version_match.group(1), s_name)
            except (KeyError, AssertionError):
                log.debug(f"Unable to parse version for sample {s_name}")

            if s_name in data_by_sample:
                log.debug(f"Duplicate sample name found in {f['fn']}! Overwriting: {s_name}")
            self.add_data_source(f, s_name, module="cellranger-arc")
            data_by_sample[s_name] = parsed_data
            if len(warnings) > 0:
                warnings_by_sample[s_name] = warnings
            for plot_type in self.plots_data_by_sample.keys():
                self.plots_data_by_sample[plot_type][s_name] = plots_data_by_id[plot_type]

        data_by_sample = self.ignore_samples(data_by_sample)
        warnings_by_sample = self.ignore_samples(warnings_by_sample)
        for k in self.plots_data_by_sample.keys():
            self.plots_data_by_sample[k] = self.ignore_samples(self.plots_data_by_sample[k])

        if len(data_by_sample) == 0:
            raise ModuleNoSamplesFound

        log.info(f"Found {len(data_by_sample)} Cell Ranger ARC reports")

        # Write parsed reports data to a file
        self.write_data_file(data_by_sample, "multiqc_cellranger_arc")

        ## Add general stats table
        self.general_stats_table(data_by_sample, self.all_headers)

        # Add sections to the report
        if len(warnings_by_sample) > 0:
            self.add_section(
                name="ARC - Warnings",
                anchor="cellranger-arc-warnings",
                description="Warnings encountered during the analysis",
                plot=table.plot(
                    warnings_by_sample,
                    self.warnings_headers,
                    {
                        "namespace": "ARC",
                        "id": "cellranger-arc-warnings-table",
                        "title": "Cellranger ARC: Warnings",
                    },
                ),
            )

        self.atac_summary_table(data_by_sample, self.all_headers)
        self.gex_summary_table(data_by_sample, self.all_headers)
        if self.plots_data_by_sample:
            self.atac_plots(self.plots_data_by_sample)
            self.gex_plots(self.plots_data_by_sample)

    def parse_html_summary(self, summary):
        """
        Cell Ranger ARC report parser
        """
        parsed_data: Dict[str, Dict] = dict()
        warnings = dict()
        plots_data_by_id = dict()

        data_rows = (
            summary["joint_metrics_table"]["rows"]
            + summary["atac_sequencing_table"]["rows"]
            + summary["gex_sequencing_table"]["rows"]
            + summary["atac_cells_table"]["rows"]
            + summary["gex_cells_table"]["rows"]
            + summary["atac_mapping_table"]["rows"]
            + summary["gex_mapping_table"]["rows"]
            + summary["atac_targeting_table"]["rows"]
        )
        help_text = (
            summary["joint_metrics_helptext"]["data"]
            + summary["atac_sequencing_helptext"]["data"]
            + summary["gex_sequencing_helptext"]["data"]
            + summary["atac_cells_helptext"]["data"]
            + summary["gex_cells_helptext"]["data"]
            + summary["atac_mapping_helptext"]["data"]
            + summary["gex_mapping_helptext"]["data"]
            + summary["atac_targeting_helptext"]["data"]
        )

        parsed_data, self.all_headers = table_data_and_headers(
            data_rows,
            help_text,
        )

        # Extract warnings if any
        alarms_list = summary["alarms"].get("alarms", [])
        for alarm in alarms_list:
            if "id" not in alarm:
                continue
            warnings[alarm["title"]] = "FAIL"
            self.warnings_headers[alarm["title"]] = {
                "title": alarm["title"],
                "description": alarm["message"],
                "bgcols": {"FAIL": "#f06807"},
            }

        plots_data_by_id["tss"] = extract_plot_data(summary["atac_tss_enrichment_plot"])
        plots_data_by_id["insert_size"] = extract_plot_data(summary["atac_insert_size_plot"])
        plots_data_by_id["saturation"] = extract_plot_data(summary["gex_seq_saturation_plot"])
        plots_data_by_id["genes"] = extract_plot_data(summary["gex_genes_per_cell_plot"])

        return parsed_data, warnings, plots_data_by_id

    def general_stats_table(self, data_by_sample, data_headers):
        """
        Takes the entire data by sample, subset and add it to the basic stats table
        """
        general_cols = {
            "Estimated number of cells": "YlGn",
            "Fraction of high-quality fragments in cells": "Blues",
            "Median genes per cell": "GnBu",
            "Feature linkages detected": "PuBuGn",
            "Linked genes": "RdYlGn",
            "Linked peaks": "RdYlBu",
        }

        general_headers = subset_header(data_headers, general_cols)
        self.general_stats_addcols(data_by_sample, general_headers)

    def atac_summary_table(self, data_by_sample, data_headers):
        """
        Takes the entire data by sample, subset for atac seq & targets and cell & mapping
        stats and adds to summary section
        """
        seq_target_cols = {
            "Sequenced read pairs": "YlGn",
            "Valid barcodes": "RdPu",
            "Percent duplicates": "Blues",
            "Number of peaks": "Greens",
            "Fraction of genome in peaks": "Purples",
            "TSS enrichment score": "PuBuGn",
            "Fraction of high-quality fragments overlapping peaks": "Spectral",
        }
        cell_mapping_cols = {
            "Estimated number of cells": "YlGn",
            "Mean raw read pairs per cell": "RdPu",
            "Fraction of high-quality fragments in cells": "Blues",
            "Fraction of transposition events in peaks in cells": "Greens",
            "Median high-quality fragments per cell": "Purples",
            "Confidently mapped read pairs": "PuBuGn",
            "Non-nuclear read pairs": "Spectral",
        }
        seq_target_headers = subset_header(data_headers, seq_target_cols, "ATAC")
        cell_mapping_headers = subset_header(data_headers, cell_mapping_cols, "ATAC")

        self.add_section(
            name="ATAC - Summary stats",
            anchor="cellranger-atac-stats",
            description="ATAC: Sequencing & Targeting metrics",
            plot=table.plot(
                data_by_sample,
                seq_target_headers,
                {
                    "id": "cellranger-atac-stats-table1",
                    "title": "ATAC: Sequencing & Targeting metrics",
                },
            ),
        )
        self.add_section(
            name="ATAC - Cell & Mapping metrics",
            anchor="cellranger-atac-cell-mapping",
            description="ATAC: Cell & Mapping metrics",
            plot=table.plot(
                data_by_sample,
                cell_mapping_headers,
                {
                    "id": "cellranger-atac-stats-table2",
                    "title": "ATAC: Cell & Mapping metrics",
                },
            ),
        )

    def atac_plots(self, plots_data):
        """
        Generates plots from ATAC data
        """

        self.add_section(
            name="ATAC - TSS enrichment plot",
            anchor="atac-tss-plot",
            description="Transcription Start Site (TSS) Plot",
            helptext="The TSS profile is displayed in the plot. The y-axis scale is normalized by the minimum signal in the window.",
            plot=linegraph.plot(
                plots_data["tss"],
                {
                    "id": "mqc_atac_tss_enrichment_plot",
                    "title": "Cell Ranger ARC (ATAC): Enrichment around TSS",
                    "xlab": "Relative Position (bp from TSS)",
                    "ylab": "Relative Enrichment",
                },
            ),
        )

        self.add_section(
            name="ATAC - Insert Size Distribution",
            anchor="atac-insert-size-plot",
            description="Insert Size Distribution Plot",
            helptext="Insert size distribution of transposase accessible fragments sequenced is displayed in the plot.",
            plot=linegraph.plot(
                plots_data["insert_size"],
                {
                    "id": "mqc_atac_insert_size_plot",
                    "title": "Cell Ranger ARC (ATAC): Insert Size Distribution",
                    "xlab": "Insert Size",
                    "ylab": "Fragment Count (linear scale)",
                },
            ),
        )

    def gex_summary_table(self, data_by_sample, data_headers):
        """
        Takes the entire data by sample, subset for gex stats and adds to summary section
        """
        gex_cols = {
            "Sequenced read pairs": "YlGn",
            "Estimated number of cells": "RdPu",
            "Mean raw reads per cell": "Blues",
            "Total genes detected": "Greens",
            "Median genes per cell": "Purples",
            "Fraction of transcriptomic reads in cells": "PuBuGn",
            "Reads with TSO": "YlOrRd",
            "Valid barcodes": "Spectral",
            "Valid UMIs": "RdYlGn",
            "Median UMI counts per cell": "YlGn",
            "Percent duplicates": "RdPu",
            "Q30 bases in barcode": "Blues",
            "Q30 bases in UMI": "Greens",
            "Reads mapped to genome": "Purples",
            "Reads mapped confidently to genome": "PuBuGn",
            "Reads mapped confidently to transcriptome": "YlOrRd",
            "Reads mapped confidently to exonic regions": "Spectral",
            "Reads mapped confidently to intronic regions": "RdYlGn",
            "Reads mapped confidently to intergenic regions": "YlGn",
            "Reads mapped antisense to gene": "RdPu",
        }

        gex_headers = subset_header(data_headers, gex_cols, "GEX")
        gex_headers = set_hidden_cols(
            gex_headers,
            [
                "Percent duplicates",
                "Q30 bases in barcode",
                "Q30 bases in UMI",
                "Reads mapped to genome",
                "Reads mapped confidently to genome",
                "Reads mapped confidently to transcriptome",
                "Reads mapped confidently to exonic regions",
                "Reads mapped confidently to intronic regions",
                "Reads mapped confidently to intergenic regions",
                "Reads mapped antisense to gene",
            ],
        )
        self.add_section(
            name="GEX - Summary stats",
            anchor="cellranger-gex-stats",
            description="GEX: Gene Expression metrics",
            plot=table.plot(
                data_by_sample,
                gex_headers,
                {
                    "id": "cellranger-gex-stats-table",
                    "title": "GEX: Gene Expression metrics",
                },
            ),
        )

    def gex_plots(self, plots_data):
        """
        Generates plots from GEX data
        """

        self.add_section(
            name="GEX - Sequencing Saturation",
            anchor="gex-saturation-plot",
            description="Sequencing Saturation Plot",
            helptext="This plot shows the Percent Duplicates metric as a function of downsampled sequencing depth (measured in mean read pairs per cell), up to the observed sequencing depth. The Percent Duplicates metric is a measure of the sequencing saturation, and approaches 1.0 (100%) when all converted mRNA transcripts have been sequenced. The slope of the curve near the endpoint can be interpreted as an upper bound to the benefit to be gained from increasing the sequencing depth beyond this point.",
            plot=linegraph.plot(
                plots_data["saturation"],
                {
                    "id": "mqc_gex_saturation_plot",
                    "title": "Cell Ranger ARC (GEX): Sequencing Saturation",
                    "xlab": "Mean Read Pairs per Cell",
                    "ylab": "Percent Duplicates",
                },
            ),
        )

        self.add_section(
            name="GEX - Median Genes per Cell",
            anchor="gex-genes-plot",
            description="Median Genes per Cell Plot",
            helptext="Observed median genes per cell as a function of downsampling rate in mean read pairs per cell.The slope of the curve near the endpoint can be interpreted as an upper bound to the benefit to be gained from increasing the sequencing depth beyond this point.",
            plot=linegraph.plot(
                plots_data["genes"],
                {
                    "id": "mqc_gex_genes_plot",
                    "title": "Cell Ranger ARC (GEX): Median Genes per Cell",
                    "xlab": "Mean Read Pairs per Cell",
                    "ylab": "Median Genes per Cell",
                },
            ),
        )
