from discord_tron_master.classes.app_config import AppConfig

config = AppConfig()


class ResolutionHelper:
    resolutions = [
        # 1:1 aspect ratio
        {"width": 768, "height": 768},
        {"width": 1024, "height": 1024, "default_max": True},
        {"width": 1280, "height": 1280},
        {"width": 2048, "height": 2048},
        # SDXL Resolutions, widescreen
        {"width": 1984, "height": 512},
        {"width": 1920, "height": 512},
        {"width": 1856, "height": 512},
        {"width": 1792, "height": 576},
        {"width": 1728, "height": 576},
        {"width": 1664, "height": 576},
        {"width": 1600, "height": 640},
        {"width": 1536, "height": 640},
        {"width": 1472, "height": 704},
        {"width": 1408, "height": 704},
        {"width": 1344, "height": 704},
        {"width": 1344, "height": 768},
        {"width": 1280, "height": 768},
        {"width": 1216, "height": 832},
        {"width": 1152, "height": 832},
        {"width": 1152, "height": 896},
        {"width": 1088, "height": 896},
        {"width": 1088, "height": 960},
        {"width": 1024, "height": 960},
        # SDXL Resolutions, portrait
        {"width": 960, "height": 1024},
        {"width": 960, "height": 1088},
        {"width": 896, "height": 1088},
        {"width": 896, "height": 1152},
        {"width": 832, "height": 1152},
        {"width": 832, "height": 1216},
        {"width": 768, "height": 1280},
        {"width": 768, "height": 1344},
        {"width": 704, "height": 1408},
        {"width": 704, "height": 1472},
        {"width": 640, "height": 1536},
        {"width": 640, "height": 1600},
        {"width": 576, "height": 1664},
        {"width": 576, "height": 1728},
        {"width": 576, "height": 1792},
        {"width": 512, "height": 1856},
        {"width": 512, "height": 1920},
        {"width": 512, "height": 1984},
        # Terminus resolutions - 1152x960, 896x1152 (in sdxl section above)
        {"width": 1152, "height": 960},
    ]

    def is_valid_resolution(self, width, height):
        for res in ResolutionHelper.resolutions:
            if res["width"] == width and res["height"] == height:
                return True

    async def list_available_resolutions(self, user_id=None, resolution=None):
        resolutions = ResolutionHelper.resolutions

        if resolution is not None:
            width, height = map(int, resolution.split("x"))
            return any(
                r["width"] == width and r["height"] == height for r in resolutions
            )

        # Grouping and sorting
        grouped_resolutions = self.group_and_sort_resolutions(resolutions)

        # Resolution table creation
        resolution_table = self.create_resolution_table(grouped_resolutions, user_id)

        # Wrap the output in triple backticks for fixed-width formatting in Discord
        return f"```\n{resolution_table}\n```"

    def group_and_sort_resolutions(self, resolutions):
        # Group resolutions into three categories: Square, Landscape, and Portrait
        grouped_resolutions = {"Square": [], "Landscape": [], "Portrait": []}
        for r in resolutions:
            if r["width"] == r["height"]:
                category = "Square"
            elif r["width"] > r["height"]:
                category = "Landscape"
            else:
                category = "Portrait"
            grouped_resolutions[category].append(r)

        # Sort resolution groups by width and height
        for category, resolutions in grouped_resolutions.items():
            grouped_resolutions[category] = sorted(
                resolutions, key=lambda r: (r["width"], r["height"])
            )

        return grouped_resolutions

    def create_resolution_table(self, grouped_resolutions, user_id):
        indicator = "**"  # Indicator variable
        indicator_length = len(indicator)
        max_rows = max(len(resolutions) for resolutions in grouped_resolutions.values())
        max_field_widths = {
            ar: max(
                len(f"{r['width']}x{r['height']}") + 2 * indicator_length
                for r in resolutions
            )
            for ar, resolutions in grouped_resolutions.items()
        }

        # Generate resolution list in Markdown columns with padding
        header_row = (
            "| "
            + " | ".join(
                ar.ljust(max_field_widths[ar]) for ar in grouped_resolutions.keys()
            )
            + " |\n"
        )
        separator_row = (
            "+-"
            + "-+-".join(
                "-" * max_field_widths[ar] for ar in grouped_resolutions.keys()
            )
            + "-+\n"
        )
        resolution_table = header_row + separator_row

        for i in range(max_rows):
            row_text = "| "
            for ar, resolutions in grouped_resolutions.items():
                res_str = self.get_resolution_string(
                    resolutions, i, max_field_widths, ar, user_id, indicator
                )
                row_text += res_str + " | "
            resolution_table += row_text + "\n"

        return resolution_table

    def get_resolution_string(
        self, resolutions, i, max_field_widths, ar, user_id, indicator
    ):
        if i < len(resolutions):
            r = resolutions[i]
            current_resolution_indicator = ""
            if user_id is not None:
                user_resolution = config.get_user_setting(user_id, "resolution")
                if (
                    user_resolution is not None
                    and user_resolution["width"] == r["width"]
                    and user_resolution["height"] == r["height"]
                ):
                    current_resolution_indicator = indicator
            res_str = (
                current_resolution_indicator
                + f"{r['width']}x{r['height']}"
                + current_resolution_indicator
            )
            return res_str.ljust(max_field_widths[ar])
        else:
            return " ".ljust(max_field_widths[ar])

    def aspect_ratio(self, resolution_item: dict):
        from math import gcd

        width = resolution_item["width"]
        height = resolution_item["height"]
        # Calculate the greatest common divisor of width and height
        divisor = gcd(width, height)

        # Calculate the aspect ratio
        ratio_width = width // divisor
        ratio_height = height // divisor

        # Return the aspect ratio as a string in the format "width:height"
        return f"{ratio_width}:{ratio_height}"
