from flask import render_template, jsonify, request, session, make_response
from .auth import login_required
from app import app, db
from models import Gouvernorat
from gouvernorats_data import charger_structure
from .utils import copy_filtered_directories, verify_folder_structure, copy_all_data
import os
import shutil
import traceback
import zipfile
import tempfile
from werkzeug.utils import secure_filename
from datetime import datetime

# Charger le fichier Excel UNE SEULE FOIS au démarrage
fichier_excel = os.path.join(app.static_folder, 'Mesures.xlsx')
DATA_EXCEL = charger_structure(fichier_excel)  # Variable globale

# Configuration pour PythonAnywhere
UPLOAD_FOLDER = os.path.join(os.path.expanduser('~'), 'uploads')
ALLOWED_EXTENSIONS = {'zip'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def find_gouvernorat_folder(base_dir, gouvernorat_name):
    """
    Trouve le dossier du gouvernorat dans une structure d'arborescence
    avec une recherche insensible à la casse.
    """
    # Normaliser le nom du gouvernorat pour la comparaison
    normalized_name = gouvernorat_name.lower()
    
    # Parcourir récursivement l'arborescence
    for root, dirs, files in os.walk(base_dir):
        for dir_name in dirs:
            if dir_name.lower() == normalized_name:
                return os.path.join(root, dir_name)
    
    # Si non trouvé, vérifier le dossier racine
    for item in os.listdir(base_dir):
        item_path = os.path.join(base_dir, item)
        if os.path.isdir(item_path) and item.lower() == normalized_name:
            return item_path
    
    return None
@app.route('/admin/upload', methods=['GET','POST'])
@login_required
def admin_page():
    # Récupérer les gouvernorats depuis la base de données
    gouvernorats = Gouvernorat.query.order_by(Gouvernorat.date_upload.desc()).all()
    
    # Récupérer les paramètres de filtrage
    page = request.args.get('page', 1, type=int)
    year_filter = request.args.get('year', '')
    gouv_filter = request.args.get('gouv', '').lower()
    
    # Construire la requête avec filtres
    query = Gouvernorat.query
    
    if year_filter:
        query = query.filter(db.extract('year', Gouvernorat.date_upload) == year_filter)
    
    if gouv_filter:
        query = query.filter(Gouvernorat.gouvernorat.ilike(f'%{gouv_filter}%'))
    
    # Ordonner et paginer la requête filtrée
    gouvernorats = query.order_by(Gouvernorat.date_upload.desc()).paginate(
        page=page,
        per_page=2,
        error_out=False
    )

    # Passer les données au template
    response = make_response(render_template(
        'admin.html', 
         data=DATA_EXCEL,
        gouvernorats=gouvernorats  # Ajout des données de la base de données
    ))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/admin/handle_upload', methods=['POST'])
@login_required
def handle_upload():
    temp_dir = None
    try:
        if 'zip_file' not in request.files:
            return jsonify({'success': False, 'message': 'Aucun fichier sélectionné'}), 400
            
        file = request.files['zip_file']
        gouvernorat = request.form.get('gouvernorat')
        
        if not gouvernorat or file.filename == '':
            return jsonify({'success': False, 'message': 'Données manquantes'}), 400

        if not allowed_file(file.filename):
            return jsonify({'success': False, 'message': 'Type de fichier non autorisé. Seuls les fichiers ZIP sont acceptés.'}), 400

        # Créer un dossier temporaire pour l'extraction
        temp_dir = tempfile.mkdtemp()
        zip_path = os.path.join(temp_dir, secure_filename(file.filename))
        file.save(zip_path)

        # Décompresser le fichier
        extracted_dir = os.path.join(temp_dir, 'extracted')
        os.makedirs(extracted_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extracted_dir)

        # Rechercher le dossier du gouvernorat
        source_path = find_gouvernorat_folder(extracted_dir, gouvernorat)
        
        if not source_path:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return jsonify({
                'success': False,
                'message': f"Aucun dossier nommé '{gouvernorat}' trouvé dans l'archive. "
                           f"Veuillez vérifier que le ZIP contient un dossier avec ce nom."
            }), 400

        # Vérifier que le dernier dossier est bien le gouvernorat
        last_folder = os.path.basename(os.path.normpath(source_path)).upper()
        if last_folder != gouvernorat.upper():
            shutil.rmtree(temp_dir, ignore_errors=True)
            return jsonify({
                'success': False,
                'message': f"Le dossier principal doit être '{gouvernorat}'. "
                           f"Vous avez téléversé un dossier nommé: '{last_folder}'"
            }), 400

        # Vérifications des prérequis
        stats_gouv_path = os.path.join(source_path, 'Statistiques Gouvernorat')
        if not os.path.exists(stats_gouv_path):
            return jsonify({'success': False, 'message': "Le dossier 'Statistiques Gouvernorat' est manquant."}), 400

        errors = []

        # Vérification du fichier Autres Indicateurs
        indicateurs_files = [
            'Autres Indicateurs.xls',
            'Autres Indicateurs.xlsx',
            'Autres indicateurs.xls',
            'Autres indicateurs.xlsx'
        ]
        indicateurs_found = any(os.path.exists(os.path.join(stats_gouv_path, f)) for f in indicateurs_files)
        if not indicateurs_found:
            errors.append("manque Autres Indicateurs")

        # Vérification du dossier SHP GOUVERNORAT
        shp_gouv_src = os.path.join(stats_gouv_path, 'SHP GOUVERNORAT')
        if not os.path.exists(shp_gouv_src) or len(os.listdir(shp_gouv_src)) == 0:
            errors.append("le dossier SHP GOUVERNORAT est vide")

        # Vérification des délégations
        for item in os.listdir(source_path):
            item_path = os.path.join(source_path, item)
            if os.path.isdir(item_path) and item != 'Statistiques Gouvernorat':
                # Vérification SHP Délégations
                shp_deleg_src = os.path.join(item_path, 'SHP Délégations')
                if not os.path.exists(shp_deleg_src) or len(os.listdir(shp_deleg_src)) == 0:
                    errors.append(f"le dossier SHP Délégations pour {item} est vide")
                # Vérification SHP Secteurs
                shp_secteurs_src = os.path.join(item_path, 'SHP Secteurs')
                if not os.path.exists(shp_secteurs_src) or len(os.listdir(shp_secteurs_src)) == 0:
                    errors.append(f"le dossier SHP Secteurs pour {item} est vide")

        if errors:
            error_message = "Le dossier téléversé est incomplet :\n* " + "\n* ".join(errors)
            return jsonify({'success': False, 'message': error_message}), 400

        # Création du dossier cible dans static/Uploads
        uploads_dir = os.path.join(app.static_folder, 'Uploads')
        if not os.path.exists(uploads_dir):
            os.makedirs(uploads_dir)

        now = datetime.now()
        folder_name = f"{gouvernorat}_{now.strftime('%Y-%m-%d')}"
        target_dir = os.path.join(uploads_dir, folder_name)
        os.makedirs(target_dir, exist_ok=True)

        # Chemin vers le dossier Statistiques Gouvernorat
        stats_gouv_path = os.path.join(source_path, 'Statistiques Gouvernorat')
        
        # 1. Copier le dossier SHP GOUVERNORAT
        shp_gouv_src = os.path.join(stats_gouv_path, 'SHP GOUVERNORAT')
        shp_gouv_dest = os.path.join(target_dir, 'SHP GOUVERNORAT')
        if os.path.exists(shp_gouv_src):
            copy_filtered_directories(shp_gouv_src, shp_gouv_dest)

        # 2. Copier le fichier Autres Indicateurs.xls (avec différentes extensions possibles)
        indicateurs_copie = False
        for filename in indicateurs_files:
            indicateurs_src = os.path.join(stats_gouv_path, filename)
            if os.path.exists(indicateurs_src):
                shutil.copy2(indicateurs_src, os.path.join(target_dir, filename))
                indicateurs_copie = True
                break
        
        if not indicateurs_copie:
            return jsonify({
                'success': False, 
                'message': 'Fichier Autres Indicateurs non trouvé dans le dossier source'
            }), 400

        # 3. Parcourir les sous-dossiers (délégations) sauf Statistiques Gouvernorat
        for item in os.listdir(source_path):
            item_path = os.path.join(source_path, item)
            if os.path.isdir(item_path) and item != 'Statistiques Gouvernorat':
                # Créer un sous-dossier pour la délégation
                delegation_dir = os.path.join(target_dir, item)
                os.makedirs(delegation_dir, exist_ok=True)
                
                # Copier SHP Délégations
                shp_deleg_src = os.path.join(item_path, 'SHP Délégations')
                shp_deleg_dest = os.path.join(delegation_dir, 'SHP Délégations')
                if os.path.exists(shp_deleg_src):
                    copy_filtered_directories(shp_deleg_src, shp_deleg_dest)
                
                # Copier SHP Secteurs
                shp_secteurs_src = os.path.join(item_path, 'SHP Secteurs')
                shp_secteurs_dest = os.path.join(delegation_dir, 'SHP Secteurs')
                if os.path.exists(shp_secteurs_src):
                    copy_filtered_directories(shp_secteurs_src, shp_secteurs_dest)

        # Enregistrement en base de données
        new_gouv = Gouvernorat(
            gouvernorat=gouvernorat,
            date_upload=now,
            dossier_copie=target_dir,
            dossier_origine=os.path.basename(file.filename)  )# Stocker le nom du fichier original
        db.session.add(new_gouv)
        db.session.commit()

        # Nettoyage des fichiers temporaires
        shutil.rmtree(temp_dir, ignore_errors=True)

        return jsonify({
            'success': True, 
            'message': 'Dossier créé avec succès!',
            'dossier': target_dir,
            'dossier_origine': os.path.basename(file.filename),
            'gouvernorat': new_gouv.gouvernorat,
            'date_upload': new_gouv.date_upload.strftime('%Y-%m-%d'),
            'id': new_gouv.id,
            'visible': new_gouv.visible
        })

    except Exception as e:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'success': False, 'message': f'Erreur: {str(e)}'}), 500

@app.route('/admin/toggle_visibility/<int:gouv_id>', methods=['POST'])
@login_required
def toggle_visibility(gouv_id):
    try:
        data = request.get_json()
        visible = data.get('visible', False)
        
        gouv = Gouvernorat.query.get_or_404(gouv_id)
        gouv.visible = visible
        db.session.commit()
        
        return jsonify({'success': True, 'visible': gouv.visible})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/update_visibility', methods=['POST'])
@login_required
def update_visibility():
    data = request.get_json()
    gouv_id = data['id']
    new_visibility = data['visible']
    
    try:
        gouv = Gouvernorat.query.get(gouv_id)
        if gouv:
            gouv.visible = new_visibility
            db.session.commit()
            return jsonify({'success': True})
        return jsonify({'success': False, 'error': 'Gouvernorat non trouvé'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/update_gouvernorat/<int:id>', methods=['POST'])
@login_required
def update_gouvernorat(id):
    temp_dir = None
    try:
        # Récupérer le gouvernorat
        gouvernorat = Gouvernorat.query.get(id)
        if not gouvernorat:
            return jsonify({'success': False, 'message': 'Gouvernorat non trouvé'}), 404

        # Récupérer les données du formulaire
        new_date_str = request.form.get('date')
        file = request.files.get('zip_file')

        # Validation de base
        if not new_date_str:
            return jsonify({'success': False, 'message': 'Date manquante'}), 400

        try:
            new_date = datetime.strptime(new_date_str, '%Y-%m-%d')
        except ValueError:
            return jsonify({'success': False, 'message': 'Format de date invalide. Utilisez YYYY-MM-DD'}), 400

        needs_copy = False
        new_source_path = None
        new_zip_filename = None

        # Si un nouveau fichier est fourni
        if file and file.filename != '':
            if not allowed_file(file.filename):
                return jsonify({
                    'success': False, 
                    'message': 'Type de fichier non autorisé. Seuls les fichiers ZIP sont acceptés.'
                }), 400

            # Créer un dossier temporaire
            temp_dir = tempfile.mkdtemp()
            zip_path = os.path.join(temp_dir, secure_filename(file.filename))
            file.save(zip_path)

            # Décompresser le fichier
            extracted_dir = os.path.join(temp_dir, 'extracted')
            os.makedirs(extracted_dir, exist_ok=True)
            try:
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(extracted_dir)
            except zipfile.BadZipFile:
                return jsonify({
                    'success': False, 
                    'message': 'Fichier ZIP corrompu ou invalide'
                }), 400

            # Rechercher le dossier du gouvernorat
            new_source_path = find_gouvernorat_folder(extracted_dir, gouvernorat.gouvernorat)
            
            if not new_source_path:
                return jsonify({
                    'success': False,
                    'message': f"Aucun dossier nommé '{gouvernorat.gouvernorat}' trouvé dans l'archive."
                }), 400

            # Vérifier que le dernier dossier est bien le gouvernorat
            last_folder = os.path.basename(os.path.normpath(new_source_path))
            if last_folder.upper() != gouvernorat.gouvernorat.upper():
                return jsonify({
                    'success': False,
                    'message': f"Le dossier principal doit être '{gouvernorat.gouvernorat}'. Trouvé: '{last_folder}'"
                }), 400

            # Vérifier la structure du dossier
            errors = verify_folder_structure(new_source_path, gouvernorat.gouvernorat)
            if errors:
                return jsonify({'success': False, 'message': errors}), 400
            
            needs_copy = True
            new_zip_filename = file.filename

        # Création du dossier cible
        uploads_dir = os.path.join(app.static_folder, 'Uploads')
        old_target_dir = gouvernorat.dossier_copie
        folder_name = f"{gouvernorat.gouvernorat}_{new_date.strftime('%Y-%m-%d')}"
        new_target_dir = os.path.join(uploads_dir, folder_name)

        if needs_copy:
            # Si le nom du dossier change, supprimer l'ancien dossier
            if old_target_dir != new_target_dir and os.path.exists(old_target_dir):
                shutil.rmtree(old_target_dir)
            # Supprimer le dossier cible s'il existe déjà (pour éviter les conflits)
            if os.path.exists(new_target_dir):
                shutil.rmtree(new_target_dir)
            os.makedirs(new_target_dir)
            copy_all_data(new_source_path, new_target_dir)
            # Mettre à jour les chemins
            gouvernorat.dossier_copie = new_target_dir
            gouvernorat.dossier_origine = new_zip_filename
        else:
            # Si seule la date a changé, renommer le dossier existant
            if old_target_dir != new_target_dir and os.path.exists(old_target_dir):
                shutil.move(old_target_dir, new_target_dir)
                gouvernorat.dossier_copie = new_target_dir

        # Mettre à jour la date dans tous les cas
        gouvernorat.date_upload = new_date
        db.session.commit()

        # Nettoyage des temporaires
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

        return jsonify({
            'success': True,
            'message': 'Modifications enregistrées',
            'id': gouvernorat.id,
            'gouvernorat': gouvernorat.gouvernorat,
            'date_upload': gouvernorat.date_upload.strftime('%Y-%m-%d'),
            'dossier_origine': gouvernorat.dossier_origine
        })

    except Exception as e:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        db.session.rollback()
        app.logger.error(f"Erreur dans update_gouvernorat: {str(e)}")
        app.logger.error(traceback.format_exc())
        return jsonify({
            'success': False, 
            'message': f'Erreur serveur: {str(e)}'
        }), 500
@app.route('/get_gouvernorat/<int:id>')
@login_required
def get_gouvernorat(id):
    gouv = Gouvernorat.query.get_or_404(id)
    return jsonify({
        'gouvernorat': gouv.gouvernorat,
        'date_upload': gouv.date_upload.strftime('%Y-%m-%d'),
        'dossier_origine': gouv.dossier_origine
    })

@app.route('/delete_gouvernorat/<int:id>', methods=['DELETE'])
@login_required
def delete_gouvernorat(id):
    try:
        gouvernorat = Gouvernorat.query.get_or_404(id)
        # Supprimer le dossier dans static/Uploads s'il existe
        if gouvernorat.dossier_copie and os.path.exists(gouvernorat.dossier_copie):
            shutil.rmtree(gouvernorat.dossier_copie)
        db.session.delete(gouvernorat)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Gouvernorat supprimé'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'Erreur: {str(e)}'}), 500