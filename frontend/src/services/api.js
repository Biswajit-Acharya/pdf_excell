import axios from 'axios';

const API_BASE_URL = 'https://pdf-excell.onrender.com/api';

export const convertPdf = async (file, conversionMode, expectedFieldsCount, expectedFieldsJson, password) => {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('conversion_mode', conversionMode);
    
    if (expectedFieldsCount) formData.append('expected_fields_count', expectedFieldsCount);
    if (expectedFieldsJson) formData.append('expected_fields_json', expectedFieldsJson);
    if (password) formData.append('password', password);

    const response = await axios.post(`${API_BASE_URL}/convert`, formData, {
        headers: {
            'Content-Type': 'multipart/form-data',
        },
    });
    return response.data;
};

export const analyzePdf = async (file, conversionMode, password) => {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('conversion_mode', conversionMode);
    if (password) formData.append('password', password);

    const response = await axios.post(`${API_BASE_URL}/analyze`, formData, {
        headers: {
            'Content-Type': 'multipart/form-data',
        },
    });
    return response.data;
};

export const exportSelection = async (conversionId, selectedFieldIds = [], exportAll = false) => {
    const formData = new FormData();
    formData.append('selected_field_ids_json', JSON.stringify(selectedFieldIds));
    formData.append('export_all', exportAll ? 'true' : 'false');

    const response = await axios.post(`${API_BASE_URL}/export/${conversionId}`, formData);
    return response.data;
};

export const getDownloadUrl = (conversionId) => {
    return `${API_BASE_URL}/download/${conversionId}`;
};
