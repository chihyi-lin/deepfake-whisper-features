import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import torch
import yaml

from src.datasets.detection_dataset import DetectionDataset
from src.models import models
from src.trainer import GDTrainer
from src.commons import set_seed
import os

def save_model(
    model: torch.nn.Module,
    loss_model,
    model_dir: Union[Path, str],
    name: str,
) -> None:
    full_model_dir = Path(f"{model_dir}/{name}")
    full_model_dir.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), f"{full_model_dir}/ckpt.pth")
    if loss_model != None:
        torch.save(loss_model.state_dict(), f"{full_model_dir}/ocsoftmax_ckpt.pth")    


def get_datasets(
    datasets_paths: List[Union[Path, str]],
    amount_to_use: Tuple[Optional[int], Optional[int]],
) -> Tuple[DetectionDataset, DetectionDataset]:
    data_train = DetectionDataset(
        asvspoof_path=datasets_paths[0],
        subset="train",
        reduced_number=amount_to_use[0],
        oversample=True,
    )
    data_test = DetectionDataset(
        asvspoof_path=datasets_paths[0],
        subset="test",
        reduced_number=amount_to_use[1],
        oversample=True,
    )

    return data_train, data_test


def train_nn(
    datasets_paths: List[Union[Path, str]],
    batch_size: int,
    epochs: int,
    device: str,
    config: Dict,
    model_dir: Optional[Path] = None,
    amount_to_use: Tuple[Optional[int], Optional[int]] = (None, None),
    config_save_path: str = "configs",
    add_loss: str = None,
    current_time: str = None
) -> Tuple[str, str]:
    logging.info("Loading data...")
    model_config = config["model"]
    model_name, model_parameters = model_config["name"], model_config["parameters"]
    optimizer_config = model_config["optimizer"]

    checkpoint_path = ""
    loss_model_checkpoint_path = None

    data_train, data_test = get_datasets(
        datasets_paths=datasets_paths,
        amount_to_use=amount_to_use,
    )

    current_model = models.get_model(
        model_name=model_name,
        config=model_parameters,
        device=device,
    )

    # If provided weights, apply corresponding ones (from an appropriate fold)
    model_path = config["checkpoint"]["path"]
    if model_path:
        current_model.load_state_dict(torch.load(model_path))
        logging.info(
            f"Finetuning '{model_name}' model, weights path: '{model_path}', on {len(data_train)} audio files."
        )
        if config["model"]["parameters"].get("freeze_encoder"):
            for param in current_model.whisper_model.parameters():
                param.requires_grad = False
    else:
        logging.info(f"Training '{model_name}' model on {len(data_train)} audio files.")
    current_model = current_model.to(device)

    use_scheduler = "rawnet3" in model_name.lower()

    current_model, loss_model = GDTrainer(
        device=device,
        batch_size=batch_size,
        epochs=epochs,
        optimizer_kwargs=optimizer_config,
        use_scheduler=use_scheduler,
        add_loss=add_loss 
    ).train(
        dataset=data_train,
        model=current_model,
        test_dataset=data_test,
    )

    if model_dir is not None:
        save_name = f"model__{model_name}__{current_time}"
        save_model(
            model=current_model,
            loss_model=loss_model,
            model_dir=model_dir,
            name=save_name,
        )
        checkpoint_path = str(model_dir.resolve() / save_name / "ckpt.pth")
        if loss_model != None:
            loss_model_checkpoint_path = str(model_dir.resolve() / save_name / "ocsoftmax_ckpt.pth")
        else:
            loss_model_checkpoint_path = None

    # Save config for testing
    if model_dir is not None:
        config["checkpoint"] = {"path": checkpoint_path}
        if loss_model != None:
            config["loss_model_checkpoint"] = {"path": loss_model_checkpoint_path}
        config_name = f"model__{model_name}__{current_time}.yaml"
        config_save_path = str(Path(config_save_path) / config_name)
        with open(config_save_path, "w") as f:
            yaml.dump(config, f)
        logging.info("Test config saved at location '{}'!".format(config_save_path))
    return config_save_path, checkpoint_path, loss_model_checkpoint_path


def main(args):
    LOGGER = logging.getLogger()
    LOGGER.setLevel(logging.INFO)

    ch = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    ch.setFormatter(formatter)
    LOGGER.addHandler(ch)
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)

    with open(args.config, "r") as f:
        config = yaml.safe_load(f)

    seed = config["data"].get("seed", 42)
    # fix all seeds
    set_seed(seed)

    if not args.cpu and torch.cuda.is_available():
        device = "cuda"
    else:
        device = "cpu"

    model_dir = Path(args.ckpt)
    model_dir.mkdir(parents=True, exist_ok=True)

    train_nn(
        datasets_paths=[
            args.asv_path,
        ],
        device=device,
        amount_to_use=(args.train_amount, args.test_amount),
        batch_size=args.batch_size,
        epochs=args.epochs,
        model_dir=model_dir,
        config=config,
        add_loss=args.add_loss
    )


def parse_args():
    parser = argparse.ArgumentParser()

    ASVSPOOF_DATASET_PATH = "datasets/ASVspoof2021/DF"

    parser.add_argument(
        "--asv_path",
        type=str,
        default=ASVSPOOF_DATASET_PATH,
        help="Path to ASVspoof2021 dataset directory",
    )

    default_model_config = "config.yaml"
    parser.add_argument(
        "--config",
        help="Model config file path (default: config.yaml)",
        type=str,
        default=default_model_config,
    )

    default_train_amount = None
    parser.add_argument(
        "--train_amount",
        "-a",
        help=f"Amount of files to load for training.",
        type=int,
        default=default_train_amount,
    )

    default_test_amount = None
    parser.add_argument(
        "--test_amount",
        "-ta",
        help=f"Amount of files to load for testing.",
        type=int,
        default=default_test_amount,
    )

    default_batch_size = 8
    parser.add_argument(
        "--batch_size",
        "-b",
        help=f"Batch size (default: {default_batch_size}).",
        type=int,
        default=default_batch_size,
    )

    default_epochs = 10
    parser.add_argument(
        "--epochs",
        "-e",
        help=f"Epochs (default: {default_epochs}).",
        type=int,
        default=default_epochs,
    )

    default_model_dir = "trained_models"
    parser.add_argument(
        "--ckpt",
        help=f"Checkpoint directory (default: {default_model_dir}).",
        type=str,
        default=default_model_dir,
    )

    parser.add_argument("--cpu", "-c", help="Force using cpu?", action="store_true")

    default_add_loss = None
    parser.add_argument('--add_loss',
                         "-l", 
                         help=f"ocsoftmax for one-class training (default: {default_add_loss})", 
                         type=str, 
                         default=default_add_loss,
                         )


    return parser.parse_args()


if __name__ == "__main__":
    main(parse_args())
