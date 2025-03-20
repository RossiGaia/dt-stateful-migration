import matplotlib.pyplot as plt
import pandas as pd
import datetime


def generate_chart(timestamps):

    """Generates a chart from the recorded timestamps."""
    # Convert timestamps into DataFrame
    df = pd.DataFrame(
        list(timestamps), columns=["Operation", "Start Time", "End Time"]
    )

    df["Duration"] = (df["End Time"] - df["Start Time"]).dt.total_seconds()

    # Normalize time for plotting (seconds from start)
    df["Normalized Start"] = (
        df["Start Time"] - df["Start Time"].min()
    ).dt.total_seconds()

    # Plot the Gantt chart
    fig_width = 8
    fig_height = len(df) * 0.6
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    ax.barh(
        df["Operation"], df["Duration"], left=df["Normalized Start"], color="skyblue"
    )

    # Formatting the plot
    ax.set_xlabel("Time (seconds from start)")
    ax.set_title("Migration Process Timeline")
    plt.xticks(fontsize=9)
    plt.yticks(fontsize=9)
    plt.grid(axis="x", linestyle="--", alpha=0.7)
    plt.tight_layout()
    plt.savefig("graph.png")
