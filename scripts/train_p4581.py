# Import relevant libraries and local modules

from dataclasses import dataclass
from typing import List
import numpy as np
import matplotlib.pyplot as plt

import pickle
import tyro

from models.lstm_oneshot_multistep import MultiStepLSTMMultiLayer
from models.tcn_oneshot_multistep import MultiStepTCN

from utils.paths import PLOTS_DIR, MAIN_DIRECTORY
from utils.data_preprocessing import (
    compare_feature_statistics,
    create_dataset,
    moving_average_causal_filter,
    normalise_dataset,
    split_train_test_forecast_windows,
)
from utils.dataset import SlowEarthquakeDataset
from utils.eval import record_metrics
from utils.general_functions import set_seed, set_torch_device
from utils.nn_io import save_model
from utils.nn_train import train_model
from utils.plotting import (
    PLOTS_DIR,
    plot_all_data_results,
    plot_example_sample,
    plot_metric_results,
    plot_original_vs_processed_data,
)

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
    exp: str = "p4581"
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

    smoothing_window: int = 100
    """moving average window size for data smoothing."""
    downsampling_factor: int = 50
    """factor by which to downsample the data."""
    lookback: int = 500
    """number of past observations to consider for forecasting."""
    forecast: int = 200
    """number of future observations to forecast."""
    n_forecast_windows: int = 5
    """number of forecasted windows in the test set."""

    # Model config options

    model: str = "LSTM"
    """model type to use"""
    n_variates: int = 1
    """number of variates in the dataset (e.g., univariate or multivariate)."""
    hidden_size: int = 50
    """size of the hidden layers in the LSTM model."""
    n_layers: int = 1
    """number of layers in the LSTM model."""
    kernel_size: int = 3
    """size of the kernel in the convolutional layers of the TCN model."""
    epochs: int = 500
    """number of epochs for training the model."""
    dropout: int = 0
    """fraction of neurons to drop in model"""

    # Plotting config options

    plot_title: str = (
        "Original Time Series and Model Predictions of Segment 1 sum"
    )
    """title for the plot."""
    plot_xlabel: str = "Time (days)"
    """label for the x-axis of the plot."""
    plot_ylabel: str = "Shear stress (MPa)"
    """label for the y-axis of the plot."""
    zoom_min: int = 1800
    """minimum x-axis value for zooming in on the plot."""
    zoom_max: int = 2000
    """maximum x-axis value for zooming in on the plot."""

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
dataset = SlowEarthquakeDataset([args.exp])
df = SlowEarthquakeDataset.convert_to_df(dataset, args.exp)
df_shear_stress = df["obs_shear_stress"]

# Print sample rate from df['time']
sample_rate = 1 / np.mean(np.diff(df["time"]))
print(f"Raw sample rate: {sample_rate}")

# Smooth and pre-process the data into windows
df_smoothed = moving_average_causal_filter(
    df_shear_stress, args.smoothing_window, args.downsampling_factor
)

# Print sample rate from based on the downsampling factor
downsampled_sample_rate = sample_rate * (1 / args.downsampling_factor)
print(f"Downsampled sample rate: {downsampled_sample_rate}")


# Visual sanity check: plot original vs. processed data
plot_original_vs_processed_data(
    df_shear_stress, df_smoothed, plot_type="scatter"
)

# Compare smoothed and original data statistics to ensure they are not
# statistically too different
if not compare_feature_statistics(
    df_shear_stress, df_smoothed, significance_level=0.05
):
    print(
        "Feature statistics are too different, consider changing the smoothing window or downsampling factor"
    )
    exit()  # Exit the script

# Break signal down into input (X) and output (y) windows
X, y = create_dataset(df_smoothed, args.lookback, args.forecast)

# Total number of samples
n_samples = X.shape[0]
print(f"Total number of samples: {n_samples}")

# TODO: Train, val and test split
# Split into train and test sets and normalise it
X_train, y_train, X_test, y_test = split_train_test_forecast_windows(
    X, y, args.forecast, args.n_forecast_windows
)

# Nb of samples in the train and test set
n_train_samples = X_train.shape[0]
n_test_samples = X_test.shape[0]
print(f"Number of samples in the train set: {n_train_samples}")
print(f"Number of samples in the test set: {n_test_samples}")

# Normalise the dataset only once you have split it into train and test so that
# the normalisation parameters are based only on the training set
data_dict, scaler_X, scaler_y = normalise_dataset(
    X_train, y_train, X_test, y_test
)

# FIXME: Do not understand the purpose of the normalisation apporach
# My understanding is we provide the y_scaler so that we can inverse the normalisation
# of the y_test_pred to compare it with the original y_test during the evaluation

# Plot lookback and forecast windows
plot_example_sample(
    data_dict["X_train_sc"],
    data_dict["y_train_sc"],
    0,
    args.lookback,
    args.forecast,
)


### ------ Train LSTM ------ ###

# Choose model
if args.model == "LSTM":
    model = MultiStepLSTMMultiLayer(
        args.n_variates,
        args.hidden_size,
        args.n_layers,
        args.output_size,
        device,
    )
elif args.model == "TCN":
    model = MultiStepTCN(
        args.n_variates,
        args.lookback,
        args.output_size,
        [args.hidden_size],
        args.kernel_size,
        args.dropout,
    )

# Train the model
results_dict = train_model(model, args.epochs, data_dict, scaler_y, device)

if args.optuna:
    with open(f"{MAIN_DIRECTORY}/scripts/tmp/results_dict_{args.optuna_id}.tmp", "wb") as handle:
        pickle.dump(results_dict, handle)

    args.record = False
    args.plot = False

if args.record:
    model_dir = save_model(
        model,
        df_smoothed.values[-len(y_test) :],
        results_dict,
        range(0, len(y_test)),
        model_name=f"{args.model}_lab_p4581",
        model_params=args,
    )

    record_metrics(
        model,
        {"y_test": y_test, "y_pred": results_dict["y_test_pred"]},
        "lab_p4581",
        model_dir,
    )


### ------ Plot Results ------ ###

if args.plot:
    # Plot predictions against true values
    test_start_idx = len(df_smoothed) - len(y_test)

    # TODO: Param the plo
    plot_all_data_results(
        test_start_idx,
        data_dict,
        results_dict,
        args.lookback,
        args.forecast,
        args.plot_title,
        args.plot_xlabel,
        args.plot_ylabel,
        [],
    )

    plot_all_data_results(
        test_start_idx,
        data_dict,
        results_dict,
        args.lookback,
        args.forecast,
        args.plot_title,
        args.plot_xlabel,
        args.plot_ylabel,
        args.zoom_window,
    )

    # Plot RMSE and R^2
    plot_metric_results(
        args.epochs,
        results_dict["train_rmse_list"],
        results_dict["test_rmse_list"],
        "RMSE",
    )
    plot_metric_results(
        args.epochs,
        results_dict["train_r2_list"],
        results_dict["test_r2_list"],
        "R$^2$",
    )

    # Plot 10 random samples from the test set
    # os.makedirs(PLOTS_DIR, exist_ok=True)
    for i in range(10):
        # random number between 0 and len(y_test)
        idx = np.random.randint(0, len(y_test))
        plt.figure()
        plt.plot(
            range(0, args.lookback),
            X_test[idx],
            label="Input",
        )
        plt.plot(
            range(args.lookback, args.lookback + args.forecast),
            y_test[idx],
            label="True",
        )
        # TODO: is the index aligned with the index of the y_test_pred?
        plt.plot(
            range(args.lookback, args.lookback + args.forecast),
            results_dict["y_test_pred"][idx],
            label="Prediction",
        )
        # save plot
        plt.legend()
        plt.savefig(f"{PLOTS_DIR}/sample_{i}.png")

    print(f"plots saved in {PLOTS_DIR}")
