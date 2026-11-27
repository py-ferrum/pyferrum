use crate::json::{JsonMap, JsonNumber, JsonValue};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyList, PyTuple};

pub(super) fn py_object_to_json(bound: &Bound<'_, PyAny>, depth: usize) -> PyResult<JsonValue> {
    if depth > 100 {
        return Err(PyValueError::new_err("JSON nesting is too deep"));
    }
    if bound.is_none() {
        return Ok(JsonValue::Null);
    }
    if let Ok(value) = bound.extract::<bool>() {
        return Ok(JsonValue::Bool(value));
    }
    if let Ok(value) = bound.extract::<i64>() {
        return Ok(JsonValue::Number(JsonNumber::from(value)));
    }
    if let Ok(value) = bound.extract::<u64>() {
        return Ok(JsonValue::Number(JsonNumber::from(value)));
    }
    if let Ok(value) = bound.extract::<f64>() {
        let number = JsonNumber::from_f64(value)
            .ok_or_else(|| PyValueError::new_err("JSON numbers must be finite"))?;
        return Ok(JsonValue::Number(number));
    }
    if let Ok(value) = bound.extract::<String>() {
        return Ok(JsonValue::String(value));
    }
    if let Ok(dict) = bound.cast::<PyDict>() {
        let mut output = JsonMap::new();
        for (key, value) in dict.iter() {
            let key: String = key.extract()?;
            output.insert(key, py_object_to_json(&value, depth + 1)?);
        }
        return Ok(JsonValue::Object(output));
    }
    if let Ok(list) = bound.cast::<PyList>() {
        let mut output = Vec::with_capacity(list.len());
        for value in list.iter() {
            output.push(py_object_to_json(&value, depth + 1)?);
        }
        return Ok(JsonValue::Array(output));
    }
    if let Ok(tuple) = bound.cast::<PyTuple>() {
        let mut output = Vec::with_capacity(tuple.len());
        for value in tuple.iter() {
            output.push(py_object_to_json(&value, depth + 1)?);
        }
        return Ok(JsonValue::Array(output));
    }
    Err(PyValueError::new_err(format!(
        "value of type {} is not JSON-compatible",
        bound.get_type().name()?
    )))
}

pub(super) fn json_value_to_py(py: Python<'_>, value: &JsonValue) -> PyResult<Py<PyAny>> {
    match value {
        JsonValue::Null => Ok(py.None()),
        JsonValue::Bool(value) => Ok(PyBool::new(py, *value).to_owned().into_any().unbind()),
        JsonValue::Number(value) => {
            if let Some(value) = value.as_i64() {
                Ok(value.into_pyobject(py)?.into_any().unbind())
            } else if let Some(value) = value.as_u64() {
                Ok(value.into_pyobject(py)?.into_any().unbind())
            } else if let Some(value) = value.as_f64() {
                Ok(value.into_pyobject(py)?.into_any().unbind())
            } else {
                Err(PyValueError::new_err("invalid JSON number"))
            }
        }
        JsonValue::String(value) => Ok(value.clone().into_pyobject(py)?.into_any().unbind()),
        JsonValue::Array(values) => {
            let list = PyList::empty(py);
            for value in values {
                list.append(json_value_to_py(py, value)?)
                    .map_err(|e| PyValueError::new_err(e.to_string()))?;
            }
            Ok(list.into_any().unbind())
        }
        JsonValue::Object(values) => {
            let dict = PyDict::new(py);
            for (key, value) in values {
                dict.set_item(key, json_value_to_py(py, value)?)
                    .map_err(|e| PyValueError::new_err(e.to_string()))?;
            }
            Ok(dict.into_any().unbind())
        }
    }
}
