# Import relevant libraries and local modules

import gc
import pickle
from dataclasses import dataclass

import pandas as pd
import torch
import tyro

import scripts.models.conv2dlstm_oneshot_multistep as conv2dlstm
from utils.data_preprocessing import (
    create_dataset,
    moving_average_causal_filter,
    normalise_dataset,
    split_train_test_forecast_windows,
)
from utils.dataset import SlowEarthquakeDataset
from utils.eval import record_metrics
from utils.general_functions import set_seed, set_torch_device
from utils.nn_io import save_model
from utils.nn_train import eval_model_on_test_set, train_model
from utils.paths import MAIN_DIRECTORY, PLOTS_DIR
from utils.plotting import plot_all_data_results, plot_metric_results

### ------ Parameters Definition ------ ###


@dataclass
class ExperimentConfig:
    """
    Configuration settings for the experiment,
    including general settings, data processing parameters, model specifications,
    and plotting options.
    """

    # General config options

    seed: int = 17
    """random seed for the dataset and model to ensure reproducibility."""
    exp: str = "cascadia_1to6_seg"
    """experiment name or identifier."""
    record: bool = True
    """flag to indicate whether results should be recorded."""
    plot: bool = True
    """flag to indicate whether to plot the results."""

    # Optuna config options

    optuna: bool = False
    """flag to indicate whether to use optuna for hyperparameter optimization."""
    optuna_id: int = 0
    """optuna study id for saving a study."""

    # Preprocessing config options

    smoothing_window: int = 10
    """moving average window size for data smoothing."""
    downsampling_factor: int = 1
    """factor by which to downsample the data."""
    lookback: int = 300
    """number of past observations to consider for forecasting."""
    forecast: int = 30
    """number of future observations to forecast."""
    n_forecast_windows: int = 5
    """number of forecasted windows in the test set."""
    n_validation_windows: int = 5
    """number of validation windows in the train set."""

    # Model config options

    model: str = "Conv2DLSTM"
    """model type to use"""
    hidden_size: int = 50
    """size of the hidden layers in the LSTM model."""
    kernel_size: int = 3
    """size of the kernel in the convolutional layers of the Conv2DLSTM model."""
    epochs: int = 75
    """number of epochs for training the model."""
    dropout: float = 0
    """fraction of neurons to drop in model"""

    # Plotting config options
    plot_title: str = "Original Time Series and Model Predictions"
    """title for the plot."""
    plot_xlabel: str = "Time (days)"
    """label for the x-axis of the plot."""
    plot_ylabel: str = "Displacement potency ($m^3$)"
    """label for the y-axis of the plot."""
    zoom_min: int = 3200
    """minimum x-axis value for zooming in on the plot."""
    zoom_max: int = 4000
    """maximum x-axis value for zooming in on the plot."""
    save_plots: bool = True
    """flag to indicate whether to save the plots."""

    def __post_init__(self):
        self.output_size = self.forecast
        self.zoom_window = [self.zoom_min, self.zoom_max]


args = tyro.cli(ExperimentConfig)

### ------ Set up ------ ###

# Set random seed
set_seed(args.seed)

# Set torch device
device = set_torch_device()


### ------ Load and pre-process data ------ ###

# Load dataset and convert to dataframe
columns = {}
dataset = SlowEarthquakeDataset([f"cascadia_{i}_seg" for i in range(1, 6 + 1)])
dataset.load()

for i in range(1, 6 + 1):
    ds_exp = dataset[f"cascadia_{i}_seg"]
    X, Y, t = ds_exp["X"], ds_exp["Y"], ds_exp["t"]
    columns[f"seg_{i}_avg"] = X.reshape(-1)

ts_data = pd.DataFrame(columns) / 1e8
ts_data.head()

# Smooth and pre-process the data into windows
df_smoothed = moving_average_causal_filter(
    ts_data, args.smoothing_window, args.downsampling_factor
)
X, y = create_dataset(df_smoothed, args.lookback, args.forecast)

# Split into train and test sets and normalise it
(
    X_train,
    y_train,
    X_val,
    y_val,
    X_test,
    y_test,
) = split_train_test_forecast_windows(
    X, y, args.forecast, args.n_forecast_windows, args.n_validation_windows
)
data_dict, scaler_X, scaler_y = normalise_dataset(
    X_train, y_train, X_test, y_test, X_val, y_val
)

### ------ Train Models ------ ###

# Choose model
if args.model == "Conv2DLSTM":
    model = conv2dlstm.Conv2DLSTMModel(
        n_variates=len(df_smoothed.columns),
        input_steps=args.lookback,
        output_steps=args.forecast,
        hidden_size=args.hidden_size,
        kernel_size=args.kernel_size,
    )

# Train the model
results_dict = train_model(model, args.epochs, data_dict, scaler_y, device)
results_dict = eval_model_on_test_set(
    model, results_dict, data_dict, scaler_y, device
)


if args.optuna:
    with open(
        f"{MAIN_DIRECTORY}/scripts/tmp/results_dict_{args.optuna_id}.tmp", "wb"
    ) as handle:
        pickle.dump(results_dict, handle)

    del model
    torch.cuda.empty_cache()
    gc.collect

    args.record = False
    args.plot = False

if args.record:
    model_dir = save_model(
        model,
        df_smoothed.values[-len(y_test) :],
        results_dict,
        range(0, len(y_test)),
        model_name=f"{args.model}_cascadia",
        model_params=args,
    )

    record_metrics(
        model,
        {"y_test": y_test, "y_pred": results_dict["y_test_pred"]},
        "cascadia_1to6",
        model_dir,
    )


# ### ------ Plot Results ------ ###

if args.plot:
    # Plot predictions against true values
    for idx in range(0, 6):
        plot_all_data_results(
            data_dict,
            results_dict,
            args.lookback,
            args.forecast,
            args.plot_title,
            args.plot_xlabel,
            args.plot_ylabel,
            [],
            ith_segment=idx,
            save_plot=args.save_plots,
        )

        plot_all_data_results(
            data_dict,
            results_dict,
            args.lookback,
            args.forecast,
            args.plot_title,
            args.plot_xlabel,
            args.plot_ylabel,
            args.zoom_window,
            ith_segment=idx,
            save_plot=args.save_plots,
        )

        # Plot RMSE and R^2
        plot_metric_results(
            args.epochs,
            results_dict["train_rmse_list"],
            results_dict["val_rmse_list"],
            "RMSE",
            "Validation",
            args.save_plots,
        )
        plot_metric_results(
            args.epochs,
            results_dict["train_r2_list"],
            results_dict["val_r2_list"],
            "R$^2$",
            "Validation",
            args.save_plots,
        )

        if args.save_plots:
            print(f"Plots saved in {PLOTS_DIR}")
