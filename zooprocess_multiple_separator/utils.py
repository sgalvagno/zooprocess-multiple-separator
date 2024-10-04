# -*- coding: utf-8 -*-
"""
Functions used in api.py
(which allow to keep api.py simple)
  - prediction of panoptic masks
  - conversion into separating lines through a watershed algorithm
"""

import os
import numpy as np
from scipy import ndimage as ndi
from PIL import Image

import torch
import torchvision
import torchvision.transforms.v2 as tr
import torchvision.transforms.v2.functional as trf

from skimage.measure import label
from skimage.segmentation import watershed, find_boundaries


def predict_mask_panoptic(image_path, model, processor, device, score_threshold=0.9, bottom_crop=31):
    """
    Perform the mask segmention for a given image with a panoptic model
    
    This runs the trained panoptic model, selects the masks above the score
    threshold, combines all masks and computes a distance map from the mask border,
    compute the center point of ach mask. Finally it returns variables relevant
    for the next part of the process.
    
    Args:
        image_path (str): path to the image to process.
        model, processor: Mask2Former model and processor objects.
          Should be global variables generated by warm() here.
        device: CPU or CUDA device. Also generated by warm().
        score_threshold (float): probably threshold to retain a potential mask.
        bottom_crop (int): number of pixels to crop from the bottom of the image
          (e.g. to remove the scale bar for example)
    
    Returns:
        panoptic_masks (np.ndarray): the labels of the retained masks.
        image (Image): input image, possibly after crop.
        binary_image (np.ndarray): thresholded image, 0 for background.
        dist_map (np.ndarray): array of the same dimension as the input image
          (after crop) constaining the map of the distance from each pixel to
          the edge of the sum of all retained masks.
        mask_centers (list of int): list of the coordinates of the center of 
          each retained mask.
        scores (float): average of the scores of the retained masks.
    """
  if model is None:
    raise ValueError("The model was not loaded correctly.")

  
    image = Image.open(image_path)

    # (possibly) crop the bottom of the image
    w, h = image.size
    image = trf.crop(image, 0, 0, h-bottom_crop, w)

    # create and preprocess the tensor
    img_tens = trf.to_image(image.convert("RGB"))
    ADE_MEAN = np.array([123.675, 116.280, 103.530]) / 255
    ADE_STD = np.array([58.395, 57.120, 57.375]) / 255
    preprocess = tr.Compose([
        tr.Resize((512,512)),
        tr.ToDtype(torch.float32, scale=True),
        tr.Normalize(ADE_MEAN, ADE_STD)
    ])
    img_tens = preprocess(img_tens)
    img_tens = img_tens.to(device)      # copy to GPU/CPU
    img_tens = img_tens[None, :, :, :]  # add empty dimension as for a batch
    
    # predict panoptic masks
    with torch.no_grad():
        try:
          outputs = model(img_tens)
        except Exception as e:
          raise HTTPBadRequest(reason=str(e))
    results = processor.post_process_panoptic_segmentation(outputs, target_sizes=[image.size[::-1]])[0]
    panoptic_masks = results["segmentation"].cpu().numpy()
    # plt.clf(); io.imshow(panoptic_masks); plt.show()

    # keep only those with a high enough score
    selected_masks_ids = [seg["id"]  for seg in results["segments_info"]\
      if seg["label_id"] == 1 and seg["score"]>score_threshold]
      # NB: label_id == 1 for objects, 0 for background
    
    # compute their average score, as an indication of the quality of the segmentation
    scores = [seg["score"] for seg in results["segments_info"]\
      if seg["id"] in selected_masks_ids]

    # assign everything else as background (=0)
    panoptic_masks[~ np.isin(panoptic_masks, selected_masks_ids)] = 0
    # plt.clf(); io.imshow(panoptic_masks); plt.show()

    # Now we need to detect large grey regions missed by the panoptic segmenter
    # and consider them as new masks, to improve the final segmentation

    # get binary image separating the background (0) from grey regions (1)
    gray_img = np.array(image.convert('L'))
    binary_image = (gray_img < 255).astype(float)
    # NB: using < 255 assumes the background is perfectly white
    # plt.clf(); io.imshow(gray_img); plt.show()

    # detect missing regions = grey regions outside of masks detected by the panoptic segmenter
    missing_regions = np.logical_and(panoptic_masks == 0, binary_image != 0)
    # plt.clf(); io.imshow(missing_regions); plt.show()
    missing_regions = label(missing_regions, background=0, return_num=False, connectivity=2)
    # plt.clf(); io.imshow(missing_regions); plt.show()
    
    # keep only large missing regions and add them as new masks
    missing_regions_ids, nb_pixels = np.unique(missing_regions, return_counts=True)
    max_mask_id = np.max(selected_masks_ids)
    for i in np.delete(missing_regions_ids, 0):
        # NB: do not consider region 0 which is the background
        # if large enough, add it to the masks
        if nb_pixels[i]>800:
            # TODO make the threshold number of pixels (800 here) configurable
            max_mask_id = max_mask_id+1  # increase the mask id counter
            panoptic_masks[missing_regions == missing_regions_ids[i]] = max_mask_id
            selected_masks_ids.append(max_mask_id)
    # plt.clf(); io.imshow(panoptic_masks); plt.show()
    
    # compute distance map (distance to the edge of each mask), mask centers and scores
    dist_map = np.zeros(panoptic_masks.shape)
    mask_centers = list()
    for mask_id in selected_masks_ids:
        single_mask = (panoptic_masks == mask_id).astype(int)
        # distance map
        dist = ndi.distance_transform_edt(single_mask)
        dist_map += dist
        # highest point, considered as the mask's center
        center_coords = np.unravel_index(np.argmax(dist, axis=None), dist.shape)
        mask_centers.append((center_coords[0], center_coords[1]))
    # plt.clf(); io.imshow(dist_map); plt.show()
    # mask_centers
    # TODO this should really be in the watershed function

    return panoptic_masks, image, binary_image, dist_map, mask_centers, np.mean(scores)


def get_watershed_result(mask_map, mask_centers, mask=None):
    """
    Apply the watershed algorithm on the predicted mask map, using the mask
    centers as markers, to generate lines separating the different objects.
    
    Args:
        mask_map (np.ndarray): map of the distance of each pixel to the edge of 
           the combined mask. Generated by `predict_mask_panoptic()`.
        mask_centers (list of int): list of the coordinates of the center of 
           each retained mask. Generated by `predict_mask_panoptic()`.
        mask (np.ndarray): a possible first version of the mask.
        # JOI: not sure what this is for.
      
    Returns:
        (np.ndarray) with 0 as background and 1 for lines separating two objects.
    """
    # Prepare watershed markers
    markers_mask = np.zeros(mask_map.shape, dtype=bool)
    for (x, y) in mask_centers:
        markers_mask[x, y] = True
    markers, _ = ndi.label(markers_mask)

    # Prepare watershed mask
    if mask is None:
        watershed_mask = np.zeros(mask_map.shape, dtype='int64')
        watershed_mask[mask_map > .01] = 1
    else:
        watershed_mask = mask

    # Apply watershed
    labels = watershed(
        -mask_map, markers, mask=watershed_mask, watershed_line=False
    )

    # Derive separation lines
    lines = np.zeros(labels.shape)
    unique_labels = list(np.unique(labels))
    unique_labels.remove(0)

    for value in unique_labels:
        single_shape = (labels == value).astype(int)
        boundaries = find_boundaries(
            single_shape, connectivity=2, mode='outer', background=0
        )
        boundaries[(labels == 0) | (labels == value)] = 0
        lines[boundaries == 1] = 1

    labels_with_lines = labels
    labels_with_lines[labels_with_lines == 0] = -1
    labels_with_lines[lines == 1] = 0

    return labels_with_lines
