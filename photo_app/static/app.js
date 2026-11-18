'use strict';
const fileInput = document.querySelector('#photo-input');
if (fileInput) {
  fileInput.addEventListener('change', () => {
    const file = fileInput.files[0];
    document.querySelector('#file-description').textContent = file ? file.name : 'JPEG, PNG, WebP or HEIC / HEIF';
    fileInput.setCustomValidity(file && file.size > 20 * 1024 * 1024 ? 'Choose a photo smaller than 20 MiB.' : '');
  });
  document.querySelector('#upload-form').addEventListener('submit', () => {
    document.querySelector('#upload-button').disabled = true;
    document.querySelector('#upload-status').textContent = 'Reading your photo and preparing its display copy…';
  });
}
document.querySelectorAll('.delete-form').forEach(form => {
  form.addEventListener('submit', event => {
    if (!window.confirm('Delete this photo, its private original, and all stored metadata?')) event.preventDefault();
  });
});
