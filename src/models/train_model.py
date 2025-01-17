import logging
import os
import pickle
from pathlib import Path

import gdown
import matplotlib.pyplot as plt
import numpy as np
import optuna
import torch
from azureml.core import Run
from torch import nn, optim
from torch.utils.data import random_split

# from src.data.MakeDataset import MakeDataset
from src.models.Classifier import Classifier
from src.models.Hyperparameters import Hyperparameters as hp


def train_model(
    trained_model_filepath,
    training_statistics_filepath,
    training_figures_filepath,
    use_azure=False,
    epochs=1,
    learning_rate=0.001,
    dropout_p=0.021,
    batch_size=250,
    seed=0,
    trial=None,
    save_training_results=True,
):

    # Check if there is a GPU available to use
    if torch.cuda.is_available():
        print("The code will run on GPU.")
    else:
        print("The code will run on CPU.")
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    seed = 0
    # Set the seed for reproducibility
    torch.manual_seed(seed)
    np.random.seed(seed)

    project_dir = Path(__file__).resolve().parents[2]

    run = None
    if use_azure:
        pathForData = str(project_dir) + "/data/processed/"

        gdown.download(
            "https://drive.google.com/uc?id=1c_3EFqYiO4VhF4SRfJorsY577PbmHnSy",
            pathForData,
            quiet=False,
        )
        print("Dataset created")

        # Get the experiment run context. That is, retrieve the experiment
        # run context when the script is run
        run = Run.get_context()
        run.log("Learning rate", learning_rate)
        run.log("Epochs", epochs)
        run.log("Dropout", dropout_p)

    log_fmt = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    logging.basicConfig(level=logging.INFO, format=log_fmt)
    logger = logging.getLogger(__name__)
    logger.info("Training a fish classifier")

    train_set_path = str(project_dir) + "/data/processed/training.pt"
    train_imgs, train_labels = torch.load(train_set_path)  # img, label

    # load data
    train_set = torch.utils.data.TensorDataset(train_imgs, train_labels)

    # split data in training and validation set
    train_n = int(0.85 * len(train_set))
    val_n = len(train_set) - train_n
    train_data, val_data = random_split(train_set, [train_n, val_n])
    print(f"Length of Train Data : {len(train_data)}")
    print(f"Length of Validation Data : {len(val_data)}")

    # Hyper parameters
    hype = hp().config
    batch_size = batch_size
    lr = learning_rate
    epochs = epochs

    trainloader = torch.utils.data.DataLoader(
        train_data, batch_size=batch_size, shuffle=True, num_workers=0
    )  # changed num_workers to 0 because i was getting error

    valoader = torch.utils.data.DataLoader(
        val_data, batch_size=batch_size, shuffle=True, num_workers=0
    )

    dataiter = iter(trainloader)
    images, labels = dataiter.next()

    print("Image shape", images.shape)
    print("Labels shape", labels.shape)

    # Initialize the model and transfer to GPU if available
    model = Classifier(
        hype["num_classes"],
        hype["filter1_in"],
        hype["filter1_out"],
        hype["filter2_out"],
        hype["filter3_out"],
        hype["image_height"],
        hype["image_width"],
        hype["pad"],
        hype["stride"],
        hype["kernel"],
        hype["pool"],
        hype["fc_1"],
        hype["fc_2"],
        hype["activation"],
        dropout_p,
    )
    model = model.to(device)

    criterion = nn.NLLLoss()
    optimizer = optim.Adam(model.parameters(), lr=lr)

    # Implement the training loop
    print("Start training")
    train_losses, val_losses, train_accuracies, val_accuracies = [], [], [], []
    for e in range(epochs):
        train_loss = 0
        train_correct = 0

        for images, labels in trainloader:
            # Transfering images and labels to GPU if available
            images, labels = images.to(device), labels.to(device)

            # Set model to training mode and zero
            #  gradients since they accumulated
            model.train()
            optimizer.zero_grad()

            # Make a forward pass through the network to get the logits
            log_ps = model(images)
            ps = torch.exp(log_ps)

            # Use the logits to calculate the loss
            loss = criterion(log_ps, labels.long())
            train_loss += loss.item()

            # Perform a backward pass through the network
            #  to calculate the gradients
            loss.backward()

            # Take a step with the optimizer to update the weights
            optimizer.step()

            # Keep track of how many are correctly classified
            top_p, top_class = ps.topk(1, dim=1)
            equals = top_class == labels.view(*top_class.shape)
            train_correct += equals.type(torch.FloatTensor).sum().item()
        else:
            # Compute validattion loss and accuracy
            val_loss = 0
            val_correct = 0

            # Turn off gradients for validation, saves memory and computations
            with torch.no_grad():
                model.eval()  # Sets the model to evaluation mode
                for images, labels in valoader:
                    # Transfering images and labels to GPU if available
                    images, labels = images.to(device), labels.to(device)

                    # Forward pass and compute loss
                    log_ps = model(images)
                    ps = torch.exp(log_ps)
                    val_loss += criterion(log_ps, labels.long()).item()

                    # Keep track of how many are correctly classified
                    top_p, top_class = ps.topk(1, dim=1)
                    equals = top_class == labels.view(*top_class.shape)
                    val_correct += equals.type(torch.FloatTensor).sum().item()

            # Store and print losses and accuracies
            train_losses.append(train_loss / len(trainloader))
            train_accuracies.append(train_correct / len(train_data))
            val_losses.append(val_loss / len(valoader))
            val_accuracies.append(val_correct / len(val_data))

            logger.info(
                str("Epoch: {}/{}.. ".format(e + 1, epochs))
                + str("Training Loss: {:.3f}.. ".format(train_losses[-1]))
                + str("Training Accuracy: {:.3f}.. ".format(train_accuracies[-1]))
                + str("Validation Loss: {:.3f}.. ".format(val_losses[-1]))
                + str("Validation Accuracy: {:.3f}.. ".format(val_accuracies[-1]))
            )

        if trial:
            # Report intermediate objective value
            trial.report(val_accuracies[-1], e)

            # Handle pruning based on the intermediate value
            if trial.should_prune():
                raise optuna.TrialPruned()

    # Save the training and validation losses and accuracies as a dictionary
    train_val_dict = {
        "train_losses": train_losses,
        "train_accuracies": train_accuracies,
        "val_losses": val_losses,
        "val_accuracies": val_accuracies,
    }

    if save_training_results:
        save_results(
            project_dir,
            trained_model_filepath,
            training_statistics_filepath,
            training_figures_filepath,
            model,
            train_val_dict,
            use_azure,
            run,
        )

    if use_azure and not trial:
        # Complete the run
        run.complete()
        print("Completed running the training expriment")
    return train_val_dict


def save_results(
    project_dir,
    trained_model_filepath,
    training_statistics_filepath,
    training_figures_filepath,
    model,
    train_val_dict,
    use_azure,
    run,
):
    """Saves the relevant training images, the model, and the results"""
    # Set file paths depending on running locally or on Azure
    model_path = project_dir.joinpath(trained_model_filepath)
    dict_path = project_dir.joinpath(training_statistics_filepath).joinpath(
        "train_val_dict.pickle"
    )
    l_fig_path = project_dir.joinpath(training_figures_filepath).joinpath(
        "Training_Loss.pdf"
    )
    a_fig_path = project_dir.joinpath(training_figures_filepath).joinpath(
        "Training_Accuracy.pdf"
    )

    if use_azure:
        # Update model path and make sure it exists
        os.makedirs("./outputs", exist_ok=True)
        model_path = "./outputs/" + trained_model_filepath
        os.makedirs(os.path.dirname(model_path), exist_ok=True)

        # Update dictionary path
        dict_path = (
            "./outputs/" + training_statistics_filepath + "train_val_dict.pickle"
        )
        os.makedirs(os.path.dirname(dict_path), exist_ok=True)

        # Update figure paths
        figures_path = "./outputs/" + training_figures_filepath
        os.makedirs(figures_path, exist_ok=True)
        l_fig_path = figures_path + "Training_Loss.pdf"
        a_fig_path = figures_path + "Training_Accuracy.pdf"

        # Log the training and validation losses and accuracies
        run.log_list("Train loss", train_val_dict["train_losses"])
        run.log_list("Train accuracy", train_val_dict["train_accuracies"])
        run.log_list("Validation loss", train_val_dict["val_losses"])
        run.log_list("Validation accuracy", train_val_dict["val_accuracies"])

    # Save the trained network
    torch.save(model.state_dict(), model_path)

    with open(dict_path, "wb") as f:
        # Pickle the 'train_val_dict' dictionary using
        #  the highest protocol available
        pickle.dump(train_val_dict, f, pickle.HIGHEST_PROTOCOL)

    # Plot the training loss curve
    f = plt.figure(figsize=(12, 8))
    plt.plot(train_val_dict["train_losses"], label="Training loss")
    plt.plot(train_val_dict["val_losses"], label="Validation loss")
    plt.xlabel("Epoch number")
    plt.ylabel("Loss")
    plt.legend()
    if use_azure:
        run.log_image(name="Training loss curve", plot=f)
    f.savefig(l_fig_path, bbox_inches="tight")

    # Plot the training accuracy curve
    f = plt.figure(figsize=(12, 8))
    plt.plot(train_val_dict["train_accuracies"], label="Training accuracy")
    plt.plot(train_val_dict["val_accuracies"], label="Validation accuracy")
    plt.xlabel("Epoch number")
    plt.ylabel("Accuracy")
    plt.legend()
    if use_azure:
        run.log_image(name="Training accuracy curve", plot=f)
    f.savefig(a_fig_path, bbox_inches="tight")
