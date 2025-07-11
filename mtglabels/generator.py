import argparse
import logging
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import cairosvg
import jinja2
import PyPDF2
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import mtglabels.config as config

# Set up logging
logging.basicConfig(format="[%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# Get the base directory of the script
BASE_DIR = Path(__file__).resolve().parent

# Set up the Jinja2 environment for template loading
ENV = jinja2.Environment(
    loader=jinja2.FileSystemLoader(BASE_DIR / "templates"),
    autoescape=jinja2.select_autoescape(["html", "xml"]),
)

# Retry Strategy for requests
retry_strategy = Retry(
    total=3,  # Total number of retries to allow
    status_forcelist=[
        429,
        500,
        502,
        503,
        504,
    ],  # Status codes to retry    allowed_methods=
    allowed_methods=["HEAD", "GET", "OPTIONS"],  # HTTP methods to retry
    backoff_factor=1,  # Backoff factor for retries
)
adapter = HTTPAdapter(max_retries=retry_strategy)
session = requests.Session()
session.mount("https://", adapter)  # Mount the retry strategy


class LabelGenerator:
    """
    Class for generating MTG labels.
    """

    # Default output directory for generated labels
    DEFAULT_OUTPUT_DIR = Path.cwd() / "output"

    # Millimeter decimal precision
    PRECISION = 1
    SCALE = 10 ** PRECISION

    def __init__(
        self,
        page_width: float,
        page_height: float,
        margin_horizontal: float,
        margin_vertical: float,
        label_width: float,
        label_height: float,
        label_columns: int,
        label_rows: int,
        output_dir=None,
    ):
        """
        Initialize the LabelGenerator.

        Args:
            page_width (float): Total page width
            page_height (float): Total page height
            margin_horizontal (float): Left/right page margins
            margin_vertical (float): Top/bottom page margins
            label_width (float): Label horizontal size
            label_height (float): Lable vertical size
            label_columns (int): Number of label columns
            label_rows (int): Number of label rows
            output_dir (str): The output directory for the generated labels. Defaults to DEFAULT_OUTPUT_DIR.
        """
        self.set_codes = []

        self.page_width = page_width
        self.page_height = page_height
        self.margin_horizontal = margin_horizontal
        self.margin_vertical = margin_vertical
        self.label_width = label_width
        self.label_height = label_height
        self.label_columns = label_columns
        self.label_rows = label_rows
        self.check_dimensions()
        log.debug(f"Calculated horizontal gap: {self.label_gap_horizontal:.2f}mm")
        log.debug(f"Calculated vertical gap: {self.label_gap_vertical:.2f}mm")

        self.output_dir = Path(output_dir or self.DEFAULT_OUTPUT_DIR)

        self.tmp_svg_dir = None
        self.setup_directories()

    @property
    def labels_per_sheet(self):
        return self.label_rows * self.label_columns

    @property
    def label_gap_horizontal(self):
        if self.label_columns == 0:
            return 0
        unused_space = (self.page_width - 2*self.margin_horizontal - self.label_columns*self.label_width)
        return unused_space / (self.label_columns - 1)

    @property
    def label_gap_vertical(self):
        if self.label_rows == 0:
            return 0
        unused_space = (self.page_height - 2*self.margin_vertical - self.label_rows*self.label_height)
        return unused_space / (self.label_rows - 1)

    def check_dimensions(self):
        positive_values = [
            self.page_width,
            self.page_height,
            self.label_height,
            self.label_width,
            self.label_rows,
            self.label_columns
        ]
        if not all(v > 0 for v in positive_values):
            raise ValueError("All page and label dimensions and counts must be positive")

        if not all(v >= 0 for v in [self.margin_horizontal, self.margin_vertical]):
            raise ValueError("Page margins must be positive or zero")

        required_width = self.label_columns*self.label_width + 2*self.margin_horizontal
        if required_width > self.page_width:
            raise ValueError(
                f"Page not wide enough for {self.label_columns} columns of {self.label_width}mm labels"
                f" with {self.margin_horizontal}mm left/right margins ({required_width:.1f}mm > {self.page_width:.1f}mm)"
            )

        required_height = self.label_rows*self.label_height + 2*self.margin_vertical
        if required_height > self.page_height:
            raise ValueError(
                f"Page not tall enough for {self.label_rows} rows of {self.label_height}mm labels"
                f" with {self.margin_vertical}mm top/bottom margins ({required_height:.1f}mm > {self.page_height:.1f}mm)"
            )

    def setup_directories(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_svg_dir = Path(tempfile.gettempdir()) / "mtglabels" / "svg"
        self.tmp_svg_dir.mkdir(parents=True, exist_ok=True)

    def generate_labels(self, template, sets=None, skip=0):
        """
        Generate the MTG labels.

        Args:
            sets (list): List of set codes to include. If None, all sets will be included.
            skip (int): Number of label places to skip.  Useful for partially used label sheets.
        """
        if sets:
            config.IGNORED_SETS = ()
            config.MINIMUM_SET_SIZE = 0
            config.SET_TYPES = ()
            self.set_codes = [exp.lower() for exp in sets]

        if skip < 0:
            raise ValueError("Skip count must be positive or zero")
        if skip >= self.labels_per_sheet:
            raise ValueError(f"Skip count must be less than {self.labels_per_sheet}")

        labels = self.create_set_label_data(skip=skip)
        label_batches = [
            labels[max(offset, 0):offset + self.labels_per_sheet]
            # Pretend that there are `skip` extra leading elements but don't include them.
            # This causes the first batch to be smaller by `skip` elements.
            # Note that this requires that skip < labels_per_sheet.
            for offset in range(-skip, len(labels), self.labels_per_sheet)
        ]

        ENV.filters["mm"] = lambda mm: round(mm * self.SCALE)
        try:
            template = ENV.get_template(template)
        except jinja2.TemplateNotFound:
            log.error(f"Template not found: {template}")
            template_list = "\n".join(
                ("  " + t) for t in ENV.list_templates()
            )
            log.error(f"Available templates:\n{template_list}")
            return

        for page, batch in enumerate(label_batches, start=1):
            output = template.render(
                labels=batch,
                PAGE_WIDTH=self.page_width,
                PAGE_HEIGHT=self.page_height,
                MARGIN_HORIZONTAL=self.margin_horizontal,
                MARGIN_VERTICAL=self.margin_vertical,
                LABEL_WIDTH=self.label_width,
                LABEL_HEIGHT=self.label_height,
                LABEL_COLUMNS=self.label_columns,
                LABEL_ROWS=self.label_rows,
                LABEL_GAP_HORIZONTAL=self.label_gap_horizontal,
                LABEL_GAP_VERTICAL=self.label_gap_vertical,
            )
            outfile_svg = (
                self.output_dir / f"labels-{self.labels_per_sheet}-{page:02}.svg"
            )
            outfile_pdf = (
                self.output_dir / f"labels-{self.labels_per_sheet}-{page:02}.pdf"
            )

            log.info(f"Writing {outfile_svg}...")
            with outfile_svg.open("w") as fd:
                fd.write(output)

            log.info(f"Writing {outfile_pdf}...")
            cairosvg.svg2pdf(
                url=str(outfile_svg), write_to=str(outfile_pdf), unsafe=True
            )

        combine_pdfs(self.output_dir)

    def get_set_data(self):
        """
        Fetch set data from Scryfall API.

        Returns:
            list: List of set data dictionaries.
        """

        try:
            log.info("Getting set data and icons from Scryfall")

            resp = session.get(config.API_ENDPOINT)
            resp.raise_for_status()

            data = resp.json().get("data", [])

            known_sets = {exp["code"] for exp in data}
            specified_sets = (
                {code.lower() for code in self.set_codes} if self.set_codes else set()
            )
            unknown_sets = specified_sets - known_sets

            if unknown_sets:
                log.warning("Unknown sets: %s", ", ".join(unknown_sets))

            set_data = [
                exp
                for exp in data
                if (
                    exp["code"] not in config.IGNORED_SETS
                    and exp["card_count"] >= config.MINIMUM_SET_SIZE
                    and (not config.SET_TYPES or exp["set_type"] in config.SET_TYPES)
                    and (not self.set_codes or exp["code"].lower() in specified_sets)
                )
            ]

            return set_data

        except requests.exceptions.RequestException as e:
            log.error("Error occurred while fetching set data: %s", str(e))
            return []

    def create_set_label_data(self, skip=0):
        """
        Create label data for the sets.

        Args:
            skip (int): Number of label places to skip.  Useful for partially used label sheets.

        Returns:
            list: List of label data dictionaries.
        """
        if skip < 0:
            raise ValueError("Skip count must be positive or zero")
        if skip >= self.labels_per_sheet:
            raise ValueError(f"Skip count must be less than {self.labels_per_sheet}")

        labels = []
        set_data = self.get_set_data()

        for label_num, set_info in enumerate(reversed(set_data), start=skip):
            label = set_info.copy()
            icon_url = set_info["icon_svg_uri"]
            filename = Path(icon_url).name.split("?")[0]
            temp_path = self.tmp_svg_dir / filename
            file_path = self.output_dir / filename

            icon_filename = filename
            if temp_path.exists() or file_path.exists():
                log.debug(f"Skipping download. File already exists: {icon_url}")
            else:
                try:
                    response = session.get(icon_url)
                    response.raise_for_status()
                    with temp_path.open("wb") as file:
                        file.write(response.content)
                except requests.exceptions.RequestException as e:
                    log.exception(f"Failed to download file: {icon_url}")
                    icon_filename = None

            if icon_filename is not None and not file_path.exists():
                shutil.copy(temp_path, file_path)

            label_column = (label_num % self.labels_per_sheet) % self.label_columns
            label_row = (label_num % self.labels_per_sheet) // self.label_columns

            label["name"] = config.RENAME_SETS.get(set_info["name"], set_info["name"])
            label["released_at"] = datetime.strptime(set_info["released_at"], "%Y-%m-%d").date()
            label["icon_filename"] = icon_filename
            label["x"] = self.margin_horizontal + (self.label_width + self.label_gap_horizontal) * label_column
            label["y"] = self.margin_vertical + (self.label_height + self.label_gap_vertical) * label_row
            labels.append(label)

        return labels


def combine_pdfs(output_dir):
    pdf_merger = PyPDF2.PdfMerger()

    # List all PDF files in the output directory that match your naming pattern
    pdf_files = sorted(output_dir.glob("labels-*.pdf"))

    for pdf_file in pdf_files:
        pdf_merger.append(str(pdf_file))

    # Output combined PDF
    combined_pdf_path = output_dir / "combined_labels.pdf"
    with combined_pdf_path.open("wb") as combined_pdf:
        pdf_merger.write(combined_pdf)
        log.info(f"Writing {combined_pdf_path}...")


def parse_arguments():
    """
    Parse command-line arguments.

    Returns:
        argparse.Namespace: Parsed command-line arguments.
    """
    parser = argparse.ArgumentParser(description="Generate MTG labels")
    parser.add_argument(
        "--output-dir",
        default=LabelGenerator.DEFAULT_OUTPUT_DIR,
        help="Output labels to this directory",
    )
    parser.add_argument(
        "--page-width", "--page-x",
        type=float,
        default=(8.5 * 25.4),
        help="Page width in millimeters",
    )
    parser.add_argument(
        "--page-height", "--page-y",
        type=float,
        default=(11 * 25.4),
        help="Page height in millimeters",
    )
    parser.add_argument(
        "--margin-horizontal", "--margin-x",
        type=float,
        default=4.0,
        help="Top/bottom margin in millimeters",
    )
    parser.add_argument(
        "--margin-vertical", "--margin-y",
        type=float,
        default=13.5,
        help="Top/bottom margin in millimeters",
    )
    parser.add_argument(
        "--label-width", "--label-x",
        type=float,
        default=(2+5/8)*25.4,
        help="Label width in millimeters",
    )
    parser.add_argument(
        "--label-height", "--label-y",
        type=float,
        default=25.2,
        help="Label height in millimeters",
    )
    parser.add_argument(
        "--label-columns", "--columns",
        type=int,
        default=3,
        help="Number of columns of labels on a single sheet",
    )
    parser.add_argument(
        "--label-rows", "--rows",
        type=int,
        default=10,
        help="Number of rows of labels on a single sheet",
    )
    parser.add_argument(
        "--template",
        type=str,
        default="address_labels.svg.jinja",
        help="Name of template file to use",
    )
    parser.add_argument(
        "--skip",
        type=int,
        default=0,
        help="Skip the first N label spaces, useful when reusing label sheets",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose log output",
    )
    parser.add_argument(
        "sets",
        nargs="*",
        help=(
            "Only output sets with the specified set code (e.g., MH1, NEO). "
            "This can be used multiple times. "
            "If empty, defaults to all sets."
        ),
        metavar="SET",
    )

    return parser.parse_args()


def main():
    """
    Main function for running the label generation.
    """

    try:
        args = parse_arguments()
        if args.verbose:
            log.setLevel(logging.DEBUG)
        generator = LabelGenerator(
            args.page_width,
            args.page_height,
            args.margin_horizontal,
            args.margin_vertical,
            args.label_width,
            args.label_height,
            args.label_columns,
            args.label_rows,
            output_dir=args.output_dir
        )
        generator.generate_labels(
            template=args.template,
            sets=args.sets,
            skip=args.skip,
        )
    except requests.exceptions.RequestException as e:
        log.error("Error occurred while making a request: %s", str(e))
    except Exception as e:
        log.exception("An unexpected error occurred: %s", str(e))


if __name__ == "__main__":
    main()
