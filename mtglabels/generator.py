import argparse
import logging
import shutil
from datetime import datetime
from pathlib import Path

import cairosvg
import jinja2
import requests

from config import SET_TYPES, MINIMUM_SET_SIZE, IGNORED_SETS, RENAME_SETS, API_ENDPOINT

# Set up logging
log = logging.getLogger(__name__)

# Get the base directory of the script
BASE_DIR = Path(__file__).resolve().parent

# Set up the Jinja2 environment for template loading
ENV = jinja2.Environment(
    loader=jinja2.FileSystemLoader(BASE_DIR / "templates"),
    autoescape=jinja2.select_autoescape(["html", "xml"]),
)


class LabelGenerator:
    """
    Class for generating MTG labels.
    """

    # Default output directory for generated labels
    DEFAULT_OUTPUT_DIR = Path.cwd() / "output"

    # Number of columns and rows on each label page
    COLS = 3
    ROWS = 10

    # Margins and starting positions on the label page
    MARGIN = 40  # in 1/10 mm
    START_X = MARGIN
    START_Y = MARGIN + 40

    # Paper sizes and default paper size
    PAPER_SIZES = {
        "letter": {"width": 2160, "height": 2790},
        "a4": {"width": 2100, "height": 2970},
    }
    DEFAULT_PAPER_SIZE = "letter"

    def __init__(self, paper_size=None, output_dir=None):
        """
        Initialize the LabelGenerator.

        Args:
            paper_size (str): The paper size to use for the labels. Defaults to DEFAULT_PAPER_SIZE.
            output_dir (str): The output directory for the generated labels. Defaults to DEFAULT_OUTPUT_DIR.
        """
        self.paper_size = paper_size or self.DEFAULT_PAPER_SIZE
        paper = self.PAPER_SIZES[self.paper_size]

        # Set up label generation parameters
        self.set_codes = []
        self.ignored_sets = IGNORED_SETS
        self.set_types = SET_TYPES
        self.minimum_set_size = MINIMUM_SET_SIZE

        self.width = paper["width"]
        self.height = paper["height"]

        self.delta_x = (self.width - (2 * self.MARGIN)) / self.COLS + 10
        self.delta_y = (self.height - (2 * self.MARGIN)) / self.ROWS - 18

        # Set up output directory and temporary SVG directory
        self.output_dir = Path(output_dir or self.DEFAULT_OUTPUT_DIR)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_svg_dir = Path("/tmp/mtglabels/svg")
        self.tmp_svg_dir.mkdir(parents=True, exist_ok=True)

    def generate_labels(self, sets=None):
        """
        Generate the MTG labels.

        Args:
            sets (list): List of set codes to include. If None, all sets will be included.
        """
        if sets:
            self.ignored_sets = ()
            self.minimum_set_size = 0
            self.set_types = ()
            self.set_codes = [exp.lower() for exp in sets]

        page = 1
        labels = self.create_set_label_data()
        label_batches = [
            labels[i: i + (self.ROWS * self.COLS)]
            for i in range(0, len(labels), self.ROWS * self.COLS)
        ]

        template = ENV.get_template("labels.svg")
        for batch in label_batches:
            output = template.render(labels=batch, WIDTH=self.width, HEIGHT=self.height)
            outfile_svg = self.output_dir / f"labels-{self.paper_size}-{page:02}.svg"
            outfile_pdf = self.output_dir / f"labels-{self.paper_size}-{page:02}.pdf"

            log.info(f"Writing {outfile_svg}...")
            with outfile_svg.open("w") as fd:
                fd.write(output)

            log.info(f"Writing {outfile_pdf}...")
            cairosvg.svg2pdf(url=str(outfile_svg), write_to=str(outfile_pdf), unsafe=True)

            page += 1

    def get_set_data(self):
        """
        Fetch set data from Scryfall API.

        Returns:
            list: List of set data dictionaries.
        """
        try:
            log.info("Getting set data and icons from Scryfall")

            resp = requests.get(API_ENDPOINT)
            resp.raise_for_status()

            data = resp.json().get("data", [])

            known_sets = {exp["code"] for exp in data}
            specified_sets = {code.lower() for code in self.set_codes} if self.set_codes else set()
            unknown_sets = specified_sets - known_sets

            if unknown_sets:
                log.warning("Unknown sets: %s", ", ".join(unknown_sets))

            set_data = [
                exp
                for exp in data
                if (
                    exp["code"] not in self.ignored_sets
                    and exp["card_count"] >= self.minimum_set_size
                    and (not self.set_types or exp["set_type"] in self.set_types)
                    and (not self.set_codes or exp["code"].lower() in specified_sets)
                )
            ]

            return set_data

        except requests.exceptions.RequestException as e:
            log.error("Error occurred while fetching set data: %s", str(e))
            return []

    def create_set_label_data(self):
        """
        Create label data for the sets.

        Returns:
            list: List of label data dictionaries.
        """
        labels = []
        x = self.START_X
        y = self.START_Y

        set_data = self.get_set_data()

        for exp in reversed(set_data):
            name = RENAME_SETS.get(exp["name"], exp["name"])
            icon_url = exp["icon_svg_uri"]
            filename = Path(icon_url).name.split("?")[0]
            file_path = self.tmp_svg_dir / filename

            if file_path.exists():
                log.info(f"Skipping download. File already exists: {icon_url}")
                icon_filename = filename
            else:
                try:
                    response = requests.get(icon_url)
                    response.raise_for_status()
                    with file_path.open("wb") as file:
                        file.write(response.content)
                    icon_filename = filename
                except requests.exceptions.RequestException as e:
                    log.error(f"Failed to download file: {icon_url}")
                    log.error("Error occurred while downloading file: %s", str(e))
                    icon_filename = None

            if icon_filename:
                shutil.copy(file_path, self.output_dir)
                labels.append(
                    {
                        "name": name,
                        "code": exp["code"],
                        "date": datetime.strptime(exp["released_at"], "%Y-%m-%d").date(),
                        "icon_filename": icon_filename,
                        "x": x,
                        "y": y,
                    }
                )

            y += self.delta_y

            if len(labels) % self.ROWS == 0:
                x += self.delta_x
                y = self.START_Y

            if len(labels) % (self.ROWS * self.COLS) == 0:
                x = self.START_X
                y = self.START_Y

        return labels


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
        "--paper-size",
        default=LabelGenerator.DEFAULT_PAPER_SIZE,
        choices=LabelGenerator.PAPER_SIZES.keys(),
        help='Use this paper size (default: "letter")',
    )
    parser.add_argument(
        "sets",
        nargs="*",
        help=(
            "Only output sets with the specified set code (e.g., MH1, NEO). "
            "This can be used multiple times."
        ),
        metavar="SET",
    )

    return parser.parse_args()


def main():
    """
    Main function for running the label generation.
    """
    log_format = '[%(levelname)s] %(message)s'
    logging.basicConfig(format=log_format, level=logging.INFO)

    try:
        args = parse_arguments()
        generator = LabelGenerator(args.paper_size, args.output_dir)
        generator.generate_labels(args.sets)
    except requests.exceptions.RequestException as e:
        log.error("Error occurred while making a request: %s", str(e))
    except Exception as e:
        log.exception("An unexpected error occurred: %s", str(e))


if __name__ == "__main__":
    main()
