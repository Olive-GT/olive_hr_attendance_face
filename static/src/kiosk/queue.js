/** @odoo-module **/

// Cola local de marcajes en IndexedDB.
//
// Es lo que hace que un corte de internet no cueste un dia de asistencia. El
// marcaje se guarda en el disco de la laptop ANTES de intentar mandarlo, asi
// que la unica forma de perderlo es que se pierda el disco.
//
// El borrado se hace solo cuando el servidor confirma por UUID. Si la respuesta
// se pierde a mitad de camino, el marcaje sigue en la cola y se reenvia; la
// restriccion de unicidad del servidor se encarga de que reenviar no duplique.
// Perder un marcaje es peor que mandarlo dos veces, y el diseno elige eso a
// conciencia.

const DB_NAME = "olive_face_kiosk";
const DB_VERSION = 1;
const STORE = "queue";

let dbPromise = null;

function open() {
    if (dbPromise) {
        return dbPromise;
    }
    dbPromise = new Promise((resolve, reject) => {
        const request = indexedDB.open(DB_NAME, DB_VERSION);
        request.onupgradeneeded = () => {
            const db = request.result;
            if (!db.objectStoreNames.contains(STORE)) {
                const store = db.createObjectStore(STORE, { keyPath: "uuid" });
                store.createIndex("device_time", "device_time");
            }
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
    });
    return dbPromise;
}

function tx(mode, run) {
    return open().then((db) => new Promise((resolve, reject) => {
        const transaction = db.transaction(STORE, mode);
        const store = transaction.objectStore(STORE);
        const request = run(store);
        transaction.oncomplete = () => resolve(request?.result);
        transaction.onerror = () => reject(transaction.error);
    }));
}

export async function push(punch) {
    await tx("readwrite", (store) => store.put(punch));
}

export async function all() {
    return (await tx("readonly", (store) => store.getAll())) || [];
}

export async function count() {
    return (await tx("readonly", (store) => store.count())) || 0;
}

/** Solo se borra lo que el servidor confirmo haber recibido. */
export async function drop(uuids) {
    if (!uuids?.length) {
        return;
    }
    await tx("readwrite", (store) => {
        for (const uuid of uuids) {
            store.delete(uuid);
        }
        return null;
    });
}

/**
 * Pide al navegador que no desaloje esta base.
 *
 * Sin esto, IndexedDB es "best effort": Chrome puede borrarla sin avisar cuando
 * el disco se llena, y la cola desapareceria en silencio justo cuando mas hace
 * falta. Devuelve si lo concedio, porque un `false` es informacion que hay que
 * mostrar, no un detalle tecnico.
 */
export async function requestPersistence() {
    if (!navigator.storage?.persist) {
        return false;
    }
    try {
        if (await navigator.storage.persisted()) {
            return true;
        }
        return await navigator.storage.persist();
    } catch {
        return false;
    }
}
