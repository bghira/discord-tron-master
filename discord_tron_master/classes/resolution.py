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
    ]


    def is_valid_resolution(self, width, height):
        for res in ResolutionHelper.resolutions:
            if res["width"] == width and res["height"] == height:
                return True

    async def list_available_resolutions(self, user_id=None, resolution=None):
        resolutions = ResolutionHelper.resolutions
        if resolution is not None:
            width, height = map(int, resolution.split("x"))
            if any(r["width"] == width and r["height"] == height for r in resolutions):
                return True
            else:
                return False

        indicator = "**"  # Indicator variable
        indicator_length = len(indicator)

        # Group resolutions by aspect ratio
        grouped_resolutions = {}
        for r in resolutions:
            ar = self.aspect_ratio(r)
            if ar not in grouped_resolutions:
                grouped_resolutions[ar] = []
            grouped_resolutions[ar].append(r)

        # Sort resolution groups by width and height
        for ar, resolutions in grouped_resolutions.items():
            grouped_resolutions[ar] = sorted(
                resolutions, key=lambda r: (r["width"], r["height"])
            )

        # Calculate the maximum number of rows for the table
        max_rows = max(len(resolutions) for resolutions in grouped_resolutions.values())

        # Calculate the maximum field text width for each column, including the indicator
        max_field_widths = {}
        for ar, resolutions in grouped_resolutions.items():
            max_field_widths[ar] = max(
                len(f"{r['width']}x{r['height']}") + 2 * indicator_length
                for r in resolutions
            )

        # Generate resolution list in Markdown columns with padding
        header_row = (
            "| "
            + " | ".join(
                ar.ljust(max_field_widths[ar]) for ar in grouped_resolutions.keys()
            )
            + " |\n"
        )

        # Update the separator_row generation
        separator_row = (
            "+-"
            + "-+-".join(
                "-" * (max_field_widths[ar]) for ar in grouped_resolutions.keys()
            )
            + "-+\n"
        )

        resolution_list = header_row + separator_row

        for i in range(max_rows):
            row_text = "| "
            for ar, resolutions in grouped_resolutions.items():
                if i < len(resolutions):
                    r = resolutions[i]
                    current_resolution_indicator = ""
                    if user_id is not None:
                        user_resolution = config.get_user_setting(user_id, "resolution")
                        if user_resolution is not None:
                            if (
                                user_resolution["width"] == r["width"]
                                and user_resolution["height"] == r["height"]
                            ):
                                current_resolution_indicator = indicator
                    res_str = (
                        current_resolution_indicator
                        + f"{r['width']}x{r['height']}"
                        + current_resolution_indicator
                    )
                    row_text += res_str.ljust(max_field_widths[ar]) + " | "
                else:
                    row_text += " ".ljust(max_field_widths[ar]) + " | "
            resolution_list += row_text + "\n"

        # Wrap the output in triple backticks for fixed-width formatting in Discord
        return f"```\n{resolution_list}\n```"

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
