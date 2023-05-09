from discord_tron_master.classes.app_config import AppConfig

config = AppConfig()


class ResolutionHelper:
    resolutions = [
        # 1:1 aspect ratio
        {"width": 128, "height": 128, "speed_factor": 100},
        {"width": 256, "height": 256, "speed_factor": 100},
        {"width": 512, "height": 512, "speed_factor": 100},
        {"width": 768, "height": 768, "speed_factor": 30},
        {"width": 1024, "height": 1024, "speed_factor": 30, "default_max": True},
        {"width": 2048, "height": 2048, "speed_factor": 30},
        {"width": 4096, "height": 4096, "speed_factor": 30},
        # 2:3 aspect ratio
        {"width": 128, "height": 192, "speed_factor": 80},
        {"width": 256, "height": 384, "speed_factor": 60},
        {"width": 512, "height": 768, "speed_factor": 49},
        {"width": 1024, "height": 1536, "speed_factor": 30, "default_max": True},
        {"width": 2048, "height": 3072, "speed_factor": 30},
        {"width": 4096, "height": 6144, "speed_factor": 30},
        # 3:2 aspect ratio
        {"width": 192, "height": 128, "speed_factor": 94},
        {"width": 384, "height": 256, "speed_factor": 76},
        {"width": 768, "height": 512, "speed_factor": 52},
        {"width": 1152, "height": 768, "speed_factor": 40},
        {"width": 1536, "height": 1024, "speed_factor": 30, "default_max": True},
        {"width": 3072, "height": 2048, "speed_factor": 30},
        {"width": 6144, "height": 4096, "speed_factor": 30},
        # 16:9 aspect ratio
        {"width": 256, "height": 144, "speed_factor": 40},
        {"width": 512, "height": 288, "speed_factor": 40},

        { "width": 480, "height": 640 },
        { "width": 768, "height": 1024 },
        { "width": 960, "height": 1280 },
        { "width": 1152, "height": 1536 },
        { "width": 1440, "height": 1920 },

        {"width": 1024, "height": 576, "speed_factor": 40},
        {"width": 1280, "height": 720, "speed_factor": 30},
        {"width": 1440, "height": 810, "speed_factor": 20},
        {"width": 1920, "height": 1080, "speed_factor": 20, "default_max": True},
        {"width": 2160, "height": 1440, "speed_factor": 20},
        {"width": 3840, "height": 2160, "speed_factor": 20},
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
