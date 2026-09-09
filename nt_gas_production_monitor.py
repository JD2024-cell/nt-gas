Improve the mobile layout of the NT Gas Production History chart only. Do not change the underlying data or calculation logic.

Default/load behaviour

Keep 3M as the default selected period on initial page load.
Where practical, only prepare/render the 3-month history initially. Longer periods can be rendered when the user selects 6M, 1Y or All.

Mobile chart

The current Plotly legend is poorly positioned on phone screens and overlaps/uses too much of the plotting area.
On narrow/mobile displays, move the legend below the chart in a horizontal layout rather than vertically inside the upper-right of the plot.
Keep the desktop legend/layout unchanged if it already looks good.
Reduce unnecessary Plotly margins on mobile so the chart uses more of the available screen width.
Ensure the y-axis title Production (TJ/day) remains readable.
Keep the existing field colours and stacked-area presentation.
Keep the Plotly toolbar available, but ensure it does not interfere with the legend.
The chart should use the full available container width.

Test specifically at approximately 360-430 px phone width.

Do not change any AEMO ingestion, field mapping, production calculations, missing-data handling or other dashboard components.
